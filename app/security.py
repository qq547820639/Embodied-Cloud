"""认证与安全原语：密码哈希、token 哈希、当前用户依赖。

- 密码：PBKDF2-SHA256，600k 迭代，per-user 随机 salt（stdlib 实现，无二进制依赖）。
- Session token：随机 32 字节 → 只存 SHA-256 哈希；原始 token 仅返回给客户端一次。
- 越权查询一律返回 404（不泄露资源是否存在），详情见 SECURITY.md T1。
"""

import base64
import hashlib
import hmac
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


def require_admin(user: User) -> User:
    from .models import Role

    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "admin role required")
    return user


# ---------------------------------------------------------------------------
# Workspace 凭据保护：控制面不长期保存可直接登录的明文密码（§20）
# ---------------------------------------------------------------------------
# code-server runtime 需要把密码放进容器 env，因此 runtime 侧必须有一份明文
# 副本；但控制面 DB 只保存 Fernet 加密后的密文。access endpoint 解密后返回给
# 合法 owner（短生命周期凭据的过渡设计，未来可替换为 gateway auth）。

_PREFIX = "enc:"


class WorkspaceCredentialCipher:
    def __init__(self, key: str):
        self._fernet = Fernet(_derive_key(key))

    def encrypt(self, plaintext: str) -> str:
        return _PREFIX + self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, stored: str) -> str | None:
        """解密失败（旧明文数据/密钥变更）返回 None，由调用方按明文兼容。"""
        if not stored.startswith(_PREFIX):
            return None
        try:
            return self._fernet.decrypt(stored[len(_PREFIX):].encode()).decode()
        except Exception:
            return None


def _derive_key(secret: str) -> bytes:
    if not secret:
        # 开发默认密钥（仅 mock/本地）；生产必须配置 EMBODIEDCLOUD_CREDENTIAL_KEY
        secret = "dev-only-credential-key-change-me"  # noqa: S105 开发默认值，生产必须显式配置
    digest = hashlib.sha256(("ec:cred:" + secret).encode()).digest()
    return base64.urlsafe_b64encode(digest)
