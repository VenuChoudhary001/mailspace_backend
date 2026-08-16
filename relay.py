import asyncio
import os
import logging
import ssl
import asyncpg
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from passlib.context import CryptContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMTP-Relay")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DATABASE_URL = os.environ["SUPABASE_DATABASE_URL"]  # fail fast if unset, don't hardcode
TLS_CERT_PATH = "/etc/letsencrypt/live/smtp.mailspace.shop/fullchain.pem"
TLS_KEY_PATH = "/etc/letsencrypt/live/smtp.mailspace.shop/privkey.pem"

class SecureDatabaseAuthenticator:
    """TEMPORARY TESTING AUTHENTICATOR: Bypasses database verification."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    #  CHANGE: Must be a regular 'def', not 'async def'
    def __call__(self, server, session, envelope, mechanism, auth_data):
        logger.info(f"Auth callback fired — mechanism={mechanism}, auth_data type={type(auth_data)}, repr={auth_data!r}")

        if mechanism not in ("LOGIN", "PLAIN"):
            return AuthResult(success=False)

        try:
            username = auth_data.login.decode("utf-8")
            password = auth_data.password.decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to unpack auth_data: {e}")
            return AuthResult(success=False)

        logger.info(f"Test Auth attempt: {username}")
        print(f"Test Auth attempt: {username}")
        TEST_USER = "test@mailspace.shop"
        TEST_PASS = "SuperSecret123!"

        if username == TEST_USER and password == TEST_PASS:
            logger.info(f"Test Auth SUCCESS: {username}")
            # Note: aiosmtpd automatically sets session.authenticated = True 
            # if we return success=True, so you don't even have to do this manually.
            # session.authenticated = True
            session.auth_user_id = "test-user-id"
            return AuthResult(success=True, auth_data=username)

        logger.warning(f"Test Auth FAILED for user: {username}")
        return AuthResult(success=False)
    
class CustomRelayHandler:
    """TEMPORARY TESTING HANDLER: Bypasses domain checks to accept the envelope."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        logger.info(f"handle_MAIL called — session.authenticated={getattr(session, 'authenticated', 'MISSING')}, id(session)={id(session)}")

        if not getattr(session, "authenticated", False):
            return "530 5.7.0 Authentication required"

        # Bypassing the DB domain verification query completely for the test run
        logger.info(f"Test run accepted sender address: {address}")
        envelope.mail_from = address
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        if not getattr(session, 'authenticated', False):
            return '530 5.7.0 Authentication required'
            
        print("\n" + "="*50)
        print("📥 INCOMING EMAIL CAPTURED!")
        print(f"From: {envelope.mail_from}")
        print(f"To: {envelope.rcpt_tos}")
        print("-"*50)
        print(envelope.content.decode('utf-8', errors='replace'))
        print("="*50 + "\n")
        
        return '250 OK Message accepted for delivery'

    async def forward_to_upstream(self, envelope):
        """
        Placeholder for the actual delivery leg — fill this in once SES/Resend
        is finalized. E.g. via aiosmtplib to SES's SMTP endpoint, or a direct
        API call (boto3 send_raw_email / Resend's REST API).
        """
        raise NotImplementedError("Wire up SES/Resend delivery here")


async def main():
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    authenticator = SecureDatabaseAuthenticator(db_pool)
    print("✅ Authenticator initialized and database pool created.")
    handler = CustomRelayHandler(db_pool)

    tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls_context.load_cert_chain(TLS_CERT_PATH, TLS_KEY_PATH)

    controller = Controller(
        handler,
        hostname="0.0.0.0",
        port=587,  # fronted by Postfix/nginx stream or port-forwarded to 587
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=True,   # now enforced — no plaintext creds accepted
        tls_context=tls_context,
        require_starttls=True,
    )

    controller.start()
    logger.info("SMTP Relay running on port 8025 (STARTTLS required)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down.")
        controller.stop()
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())