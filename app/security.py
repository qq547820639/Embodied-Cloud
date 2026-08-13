"""认证与安全原语：密码哈希、token 哈希、当前用户依赖。

- 密码：PBKDF2-SHA256，600k 迭代，per-user 随机 salt（stdlib 实现，无二进制依赖）。
- Session token：随机 32 字节 → 只存 SHA-256 哈希；原始 token 仅返回给客户端一次。
- 越权查询一律返回 404（不泄露资源是否存在），详情见 SECURITY.md T1。
"""

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import session_dependency
from .models import User, UserSession

PBKDF2_ITERATIONS = 600_000
_HASH_ALGO = "sha256"


def _salt_prefix(pepper: str) -> bytes:
    return ("ec:" + pepper).encode() if pepper else b""


def hash_password(password: str, pepper: str = "") -> str:
    """返回格式：pbkdf2$iterations$salt_hex$digest_hex"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        _HASH_ALGO,
        _salt_prefix(pepper) + password.encode(),
        salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str, pepper: str = "") -> bool:
    try:
        _, iterations_str, salt_hex, digest_hex = stored.split("$")
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    computed = hashlib.pbkdf2_hmac(
        _HASH_ALGO,
        _salt_prefix(pepper) + password.encode(),
        salt,
        iterations,
    )
    return hmac.compare_digest(computed, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def make_session_dependency(session_factory, settings: Settings):
    """构造 FastAPI 依赖：解析 Authorization Bearer token → User。

    未认证访问保护端点返回 401。
    """
    get_db = session_dependency(session_factory)

    def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:  # noqa: B008 依赖工厂内嵌 Depends 是 FastAPI 标准模式
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = auth[len("Bearer "):].strip()
        if not token:
            raise HTTPException(401, "missing bearer token")
        session = db.scalar(
            select(UserSession).where(UserSession.token_hash == hash_token(token))
        )
        if session is None or session.revoked:
            raise HTTPException(401, "invalid or revoked session")
        expires = (
            session.expires_at.replace(tzinfo=UTC)
            if session.expires_at.tzinfo is None
            else session.expires_at
        )
        if expires < datetime.now(UTC):
            raise HTTPException(401, "session expired")
        user = db.get(User, session.user_id)
        if user is None or not user.is_active:
            raise HTTPException(401, "user unavailable")
        return user

    return get_current_user


# ---------------------------------------------------------------------------
# Workspace 凭据保护：控制面不长期保存可直接登录的明文密码（§20/§13）
# ---------------------------------------------------------------------------
# code-server runtime 需要把密码放进容器 env，因此 runtime 侧必须有一份明文
# 副本；但控制面 DB 只保存 Fernet 加密后的密文。access endpoint 解密后返回给
# 合法 owner。
# §13 fail-closed：enc: 前缀的密文解密失败 = 密钥变更/数据损坏 → 抛异常，
# **禁止把密文当明文密码返回**；非 enc: 前缀 = 迁移期旧明文（兼容读取）。

_PREFIX = "enc:"


class CredentialDecryptError(RuntimeError):
    """密文无法解密（fail closed）：不得回退为明文读取。"""


class WorkspaceCredentialCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(_derive_key(key))

    def encrypt(self, plaintext: str) -> str:
        return _PREFIX + self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, stored: str) -> str | None:
        """解密 enc: 密文；非密文（迁移期明文）返回 None。

        密文解密失败 → 抛 CredentialDecryptError（fail closed）。
        """
        if not stored.startswith(_PREFIX):
            return None  # 旧明文（encrypt 引入前写入）
        try:
            return self._fernet.decrypt(stored[len(_PREFIX):].encode()).decode()
        except Exception as exc:
            raise CredentialDecryptError(
                "stored credential ciphertext cannot be decrypted "
                "(key rotation or corruption); refusing to return it as plaintext"
            ) from exc

    def resolve(self, stored: str | None) -> str | None:
        """access 路径统一解析：密文→明文；旧明文→原样；None→None。

        fail closed：密文解密失败抛 CredentialDecryptError。
        """
        if stored is None:
            return None
        if stored.startswith(_PREFIX):
            return self.decrypt(stored)
        return stored  # 迁移期旧明文


def _derive_key(secret: str) -> bytes:
    if not secret:
        # 开发默认密钥（仅 mock/本地）；生产必须配置
        # EMBODIEDCLOUD_WORKSPACE_CREDENTIAL_KEY（deps 启动校验拒绝生产使用）
        secret = "dev-only-credential-key-change-me"  # noqa: S105 开发默认值，生产必须显式配置
    digest = hashlib.sha256(("ec:cred:" + secret).encode()).digest()
    return base64.urlsafe_b64encode(digest)


def validate_credential_configuration(
    provider: str,
    credential_key: str,
    password_pepper: str = "",
    auto_create_tables: bool = True,
) -> None:
    """§13/§S-1 生产启动安全校验（fail-closed）。

    仅对非 mock provider 生效；mock（本地/演示）跳过全部生产校验。规则：
    - 未显式配置 workspace 凭据密钥 → 拒绝启动（禁止开发默认密钥，加密形同虚设）
    - password_pepper 为空 → 拒绝启动（生产必须配置密码 pepper，禁止退化空盐前缀）
    - auto_create_tables=true → 仅 logger.warning（不做拒绝：docker 单机 SQLite
      是合法组合，拒绝会破坏该场景；生产 compose 应显式关闭走 alembic）
    """
    if provider.lower() in {"mock"}:
        return
    if not credential_key:
        raise RuntimeError(
            "EMBODIEDCLOUD_WORKSPACE_CREDENTIAL_KEY must be explicitly configured "
            f"when provider={provider!r} (refusing to run production with the "
            "dev fallback key)"
        )
    if not password_pepper:
        raise RuntimeError(
            "EMBODIEDCLOUD_PASSWORD_PEPPER must be explicitly configured "
            f"when provider={provider!r} (refusing to run production with an "
            "empty password pepper)"
        )
    if auto_create_tables:
        logging.getLogger("embodiedcloud").warning(
            "auto_create_tables=true with provider=%r: tables will be created "
            "via SQLAlchemy metadata instead of alembic migrations; for "
            "production prefer auto_create_tables=false (docker single-host "
            "SQLite is an accepted exception)",
            provider,
        )
