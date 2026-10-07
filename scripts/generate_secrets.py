"""Run locally: python scripts/generate_secrets.py. These values are for Railway Variables."""
import secrets
print('APP_SECRET='+secrets.token_urlsafe(48))
print('ADMIN_PASSWORD='+secrets.token_urlsafe(24))
print('Save these values privately. APP_SECRET must remain unchanged across updates/restores.')
