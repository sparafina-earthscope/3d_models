"""Zip an STL and email it as an attachment.

Mail server settings come from environment variables, or from a settings file at
~/.config/terrain-app/smtp.env (KEY=value lines; environment variables take precedence).
Keep the file outside the repository so the password is never committed.

  SMTP_HOST       mail server, e.g. smtp.gmail.com (required)
  SMTP_PORT       default 587 (465 when SMTP_SECURITY=ssl)
  SMTP_SECURITY   starttls (default), ssl, or none
  SMTP_USER       login name, if the server needs one
  SMTP_PASSWORD   login password (for Gmail, an app password)
  SMTP_FROM       sender address (default SMTP_USER)
  SMTP_MAX_MB     largest email to send, in MB (default 25)
"""
import email.utils, json, os, re, smtplib, socket, ssl, tempfile, threading, zipfile
from email.message import EmailMessage

SETTINGS_FILE = os.path.expanduser('~/.config/terrain-app/smtp.env')
EMAIL_RE = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+')
CONNECT_TIMEOUT = 30    # seconds to connect and get the server's greeting
SEND_TIMEOUT = 120      # seconds per network operation once connected

_log_lock = threading.Lock()


def _read_settings_file(path):
    """KEY=value lines; allows 'export KEY=value', '# comments' and one surrounding pair of quotes."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read {path}: {getattr(e, 'strerror', None) or e}") from None
    cfg = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = (part.strip() for part in line.split('=', 1))
        if k.startswith('export '):
            k = k[len('export '):].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v = v[1:-1]
        cfg[k] = v
    return cfg


def smtp_settings():
    """Mail server settings, or None if SMTP_HOST isn't configured. Raises ValueError if they're invalid."""
    cfg = _read_settings_file(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else {}
    cfg.update({k: v for k, v in os.environ.items() if k.startswith('SMTP_')})
    if not cfg.get('SMTP_HOST'):
        return None
    security = cfg.get('SMTP_SECURITY', 'starttls').lower()
    if security not in ('starttls', 'ssl', 'none'):
        raise ValueError(f'SMTP_SECURITY must be starttls, ssl or none, not {security!r}')
    try:
        port = int(cfg.get('SMTP_PORT') or (465 if security == 'ssl' else 587))
    except ValueError:
        raise ValueError(f"SMTP_PORT must be a number, not {cfg['SMTP_PORT']!r}") from None
    try:
        max_mb = float(cfg.get('SMTP_MAX_MB') or 25)
    except ValueError:
        raise ValueError(f"SMTP_MAX_MB must be a number, not {cfg['SMTP_MAX_MB']!r}") from None
    for key in ('SMTP_USER', 'SMTP_PASSWORD'):   # smtplib logs in with ASCII only; never echo the value
        if not cfg.get(key, '').isascii():
            raise ValueError(f'{key} contains non-ASCII characters, which SMTP login does not support')
    sender = cfg.get('SMTP_FROM') or cfg.get('SMTP_USER', '')
    if not sender_ok(sender):
        raise ValueError('SMTP_FROM (or SMTP_USER) must be an email address such as you@example.org')
    return {'host': cfg['SMTP_HOST'], 'port': port, 'security': security, 'user': cfg.get('SMTP_USER', ''),
            'password': cfg.get('SMTP_PASSWORD', ''), 'sender': sender, 'max_mb': max_mb}


def valid_email(address):
    if not isinstance(address, str) or len(address) > 254 or not EMAIL_RE.fullmatch(address):
        return False
    local, _, domain = address.rpartition('@')
    return len(local) <= 64 and all(len(label) <= 63 for label in domain.split('.'))


def sender_ok(sender):
    """True for 'me@example.org' or 'Name <me@example.org>'."""
    if not sender or '\n' in sender or '\r' in sender:
        return False
    return valid_email(email.utils.parseaddr(sender)[1])


def zip_file(path):
    """Write <path>.zip next to the file and return its path."""
    zip_path = os.path.splitext(path)[0] + '.zip'
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(zip_path) or '.', suffix='.part')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(path, os.path.basename(path))
        os.chmod(tmp, 0o644)
        os.replace(tmp, zip_path)   # atomic, so a concurrent send never attaches a partial zip
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return zip_path


def log_email(path, record):
    """Append a record to the JSON list in path, creating the file if needed.

    Raises ValueError if the file isn't a JSON list, rather than overwriting it.
    """
    with _log_lock:   # sessions in the same app add records one at a time
        records = []
        if os.path.exists(path):
            with open(path) as f:
                try:
                    records = json.load(f)
                except json.JSONDecodeError as e:
                    raise ValueError(f'{path} is not valid JSON ({e})') from None
            if not isinstance(records, list):
                raise ValueError(f'{path} does not hold a JSON list')
        records.append(record)
        # mkstemp makes the file readable only by its owner, which suits a list of email addresses
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', suffix='.part')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(records, f, indent=2)
                f.write('\n')
            os.replace(tmp, path)   # atomic, so a crash never leaves a half-written file
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def encoded_mb(path):
    """Size of the file once base64-encoded in an email (76-character lines plus CRLF), in MB."""
    return os.path.getsize(path) * 4 / 3 * 78 / 76 / 1e6


def send_zip(to, zip_path, subject, body, settings=None):
    """Email zip_path as an attachment to the address `to`."""
    s = settings or smtp_settings()
    if s is None:
        raise RuntimeError('Email is not set up: SMTP_HOST is not configured')
    if not valid_email(to):
        raise ValueError(f'{to!r} is not a valid email address')

    def too_big(mb):
        return ValueError(f'The zipped model is {mb:.1f} MB once attached, over the {s["max_mb"]:g} MB email limit')
    if encoded_mb(zip_path) > s['max_mb']:     # cheap check before reading the file
        raise too_big(encoded_mb(zip_path))

    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = s['sender'], to, subject
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg['Message-ID'] = email.utils.make_msgid(domain=email.utils.parseaddr(s['sender'])[1].rpartition('@')[2])
    msg.set_content(body)
    with open(zip_path, 'rb') as f:
        msg.add_attachment(f.read(), maintype='application', subtype='zip',
                           filename=os.path.basename(zip_path))
    size_mb = len(msg.as_bytes()) / 1e6
    if size_mb > s['max_mb']:
        raise too_big(size_mb)

    context = ssl.create_default_context()
    if s['security'] == 'ssl':
        server = smtplib.SMTP_SSL(s['host'], s['port'], timeout=CONNECT_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(s['host'], s['port'], timeout=CONNECT_TIMEOUT)
    with server:
        server.sock.settimeout(SEND_TIMEOUT)
        if s['security'] == 'starttls':
            server.starttls(context=context)
        if s['user']:
            server.login(s['user'], s['password'])
        server.send_message(msg)


def _replies(ex):
    """(code, lowercase text) pairs from an SMTP error."""
    if isinstance(ex, smtplib.SMTPRecipientsRefused):
        pairs = list(ex.recipients.values())
    elif isinstance(ex, smtplib.SMTPResponseException):
        pairs = [(ex.smtp_code, ex.smtp_error)]
    else:
        return []
    out = []
    for code, text in pairs:
        text = text.decode(errors='replace') if isinstance(text, bytes) else str(text)
        out.append((code, text.lower()))
    return out


def describe_error(ex, settings=None):
    """A plain-language explanation of a failed send."""
    where = f"{settings['host']}:{settings['port']}" if settings else 'the mail server'
    replies = _replies(ex)
    if any(code == 530 or 'starttls' in text or 'authentication required' in text or 'must issue' in text
           for code, text in replies):
        return (f'{where} requires a secure connection or a login first. '
                'Check SMTP_SECURITY, SMTP_USER and SMTP_PASSWORD.')
    if any('5.2.2' in text or 'quota' in text or 'mailbox full' in text or 'storage' in text
           for _, text in replies):
        return "The recipient's mailbox is full. Try another address."
    if any(code == 552 or '5.3.4' in text or 'too large' in text or 'too big' in text or 'size' in text
           for code, text in replies):
        return 'The mail server says the email is too large. Lower SMTP_MAX_MB to its limit.'
    if any('application-specific password' in text for _, text in replies):
        return 'This account needs an app password in SMTP_PASSWORD, not its regular password (see the README).'
    if isinstance(ex, smtplib.SMTPAuthenticationError):
        return 'The mail server rejected the login. Check SMTP_USER and SMTP_PASSWORD.'
    if isinstance(ex, smtplib.SMTPRecipientsRefused):
        return 'The mail server refused to send to that address.'
    if isinstance(ex, smtplib.SMTPSenderRefused):
        return 'The mail server refused the sender address. Check SMTP_FROM.'
    if isinstance(ex, ssl.SSLError):
        return f'Could not make a secure connection to {where}. Check SMTP_SECURITY and SMTP_PORT.'
    if isinstance(ex, smtplib.SMTPNotSupportedError):
        return f'{where} does not support this login or security setting. Check SMTP_SECURITY.'
    if isinstance(ex, (socket.timeout, TimeoutError, ConnectionError, socket.gaierror, smtplib.SMTPConnectError,
                       smtplib.SMTPServerDisconnected)):
        hint = (' Port 465 needs SMTP_SECURITY=ssl.'
                if settings and settings['port'] == 465 and settings['security'] != 'ssl' else '')
        return f'Could not reach the mail server at {where}.{hint}'
    return str(ex) or type(ex).__name__
