"""Run interactively as root over SSH; credentials never appear in argv or logs."""
import getpass
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Run as root in an interactive SSH terminal.')
    os.umask(0o077)
    target = Path('/etc/catai/catai.env')
    if target.exists():
        raise SystemExit('Configuration already exists; refusing to overwrite secrets.')
    print('CatAI: credentials are hidden. No messages will be sent. Replies stay disabled.')
    token = getpass.getpass('Telegram bot token: ').strip()
    key = getpass.getpass('OpenAI API key: ').strip()
    owner = input('Owner numeric Telegram ID: ').strip()
    peer = input('Single pilot peer numeric ID: ').strip()
    owner_username = input('Owner public username (optional, without @): ').strip()
    if (not token or ':' not in token or not key or not owner.isdecimal() or
            not peer.isdecimal() or (owner == peer and owner != '0') or
            any(c.isspace() for c in token + key) or any(c in token + key for c in '\\"\x00')):
        raise SystemExit('Invalid input. Nothing saved; try again.')
    if owner_username and not all(c.isascii() and (c.isalnum() or c == '_') for c in owner_username):
        raise SystemExit('Invalid username. Nothing saved.')
    # Import installed package: fixed Telegram endpoint and safe error messages.
    from catai.app import Telegram, ServiceError
    tg = Telegram(token)
    try:
        me = tg.call('getMe')
        if not me.get('can_connect_to_business'):
            raise ServiceError('Enable Secretary Mode in BotFather.')
        if tg.call('getWebhookInfo').get('url'):
            raise ServiceError('Existing webhook detected; configuration refused.')
    except ServiceError:
        raise SystemExit('Telegram check failed. Nothing saved; verify token, bot and webhook.') from None
    values = {
        'TELEGRAM_BOT_TOKEN': token, 'MODEL_API_KEY': key,
        'OWNER_ID': owner, 'TEST_CHAT_ID': peer, 'RESPONSES_ENABLED': 'false',
        'CHAT_SCOPE': 'pilot', 'BOT_USERNAME': me['username'], 'OWNER_USERNAME': owner_username,
        'MODEL_ENDPOINT': 'https://api.openai.com/v1/chat/completions',
        'MODEL_NAME': 'gpt-6-luna', 'MODEL_PROVIDER_LABEL': 'OpenAI (GPT-6 Luna)',
        'MODEL_PRIVACY_URL': 'https://developers.openai.com/api/docs/guides/your-data',
    }
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.catai-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            for k, v in values.items():
                file.write(f'{k}="{v}"\n')
        # Atomic exclusive installation; never replace an existing configuration.
        os.link(name, target)
    finally:
        os.unlink(name)
    print('Saved root-only configuration (0600).')
    if int(owner) == 0 or int(peer) == 0:
        print('IDs still required. Service remains stopped; return to Codex to identify the pilot peers.')
        return
    print('Starting awake with replies disabled.')
    subprocess.run(['systemctl', 'enable', '--now', 'catai'], check=True)
    subprocess.run(['systemctl', 'is-active', 'catai'], check=True)
    print('Next: send /status to your bot. Do not enable replies yet.')


if __name__ == '__main__':
    main()
