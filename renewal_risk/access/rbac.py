"""
rbac.py — who may see which accounts. Enforced in the service layer, so the
REST API, Slack and alerts all obey the same rules.

access.yaml:

  roles:                       # optional; these are the built-in defaults
    admin:    {all_accounts: true, permissions: [view, bulk, assess, admin]}
    director: {permissions: [view, bulk, assess]}
    manager:  {permissions: [view, bulk, assess]}
    csm:      {permissions: [view, assess]}
    viewer:   {permissions: [view]}
  users:
    - email: pd.emea@acme.com
      role: director
      scope: {region: [EMEA, UK]}          # standard account fields -> allowed values
    - email: csm1@acme.com
      role: csm                            # no scope -> only accounts they own
    - email: vp@acme.com
      role: director
      scope: all
  domains:                                 # optional catch-all per email domain
    - {domain: acme.com, role: viewer, scope: {owner_email: self}}
  api_keys:
    - {name: bi-dashboard, key: "${ENV:RISK_API_KEY_BI}", as: ops@acme.com}

Scope rules: every key must match (AND); a list means any value (OR); the
value `self` means the user's own email. Account fields that are lists
(domain) match if any element matches. Unknown users get no access at all.

Permissions: view (single-account questions), bulk (portfolio / window
questions), assess (trigger a new paid assessment rather than read a stored
one), admin (connector tests, link review, setup checks).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass, field

DEFAULT_ROLES = {
    "admin": {"all_accounts": True, "permissions": ["view", "bulk", "assess", "admin"]},
    "director": {"permissions": ["view", "bulk", "assess"]},
    "manager": {"permissions": ["view", "bulk", "assess"]},
    "csm": {"permissions": ["view", "assess"]},
    "viewer": {"permissions": ["view"]},
}


class AccessDenied(Exception):
    pass


@dataclass
class Principal:
    email: str
    role: str
    permissions: set[str] = field(default_factory=set)
    scope: dict | str | None = None
    all_accounts: bool = False
    name: str | None = None

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise AccessDenied(f"Your role ({self.role}) does not allow '{permission}'.")

def _norm(value) -> str:
    return str(value).strip().lower()


def can_view(principal: Principal, account: dict | None) -> bool:
    if account is None or not principal.can("view"):
        return False
    if principal.all_accounts or principal.scope == "all":
        return True
    scope = principal.scope or {"owner_email": "self"}
    for fld, allowed in scope.items():
        allowed_values = allowed if isinstance(allowed, list) else [allowed]
        allowed_set = {_norm(principal.email) if a == "self" else _norm(a) for a in allowed_values}
        actual = account.get(fld)
        actual_values = actual if isinstance(actual, list) else [actual]
        if not any(v is not None and _norm(v) in allowed_set for v in actual_values):
            return False
    return True


class AccessPolicy:
    def __init__(self, access_cfg: dict):
        self.cfg = access_cfg or {}
        self.roles = {**DEFAULT_ROLES, **(self.cfg.get("roles") or {})}

    def _principal(self, email: str, entry: dict, name: str | None = None) -> Principal:
        role = entry.get("role", "viewer")
        role_cfg = self.roles.get(role)
        if role_cfg is None:
            raise AccessDenied(f"unknown role '{role}' in access.yaml")
        return Principal(
            email=email.lower(), role=role, permissions=set(role_cfg.get("permissions", [])),
            scope=entry.get("scope"), all_accounts=bool(role_cfg.get("all_accounts")), name=name,
        )

    def principal_for_email(self, email: str | None, name: str | None = None) -> Principal | None:
        if not email:
            return None
        email = email.strip().lower()
        for entry in self.cfg.get("users") or []:
            if _norm(entry.get("email", "")) == email:
                return self._principal(email, entry, name)
        domain = email.rsplit("@", 1)[-1]
        for entry in self.cfg.get("domains") or []:
            if _norm(entry.get("domain", "")) == domain:
                return self._principal(email, entry, name)
        return None

    def principal_for_api_key(self, key: str | None) -> Principal | None:
        if not key:
            return None
        for entry in self.cfg.get("api_keys") or []:
            expected = str(entry.get("key") or "")
            if expected and hmac.compare_digest(expected, key):
                return self.principal_for_email(entry.get("as"), entry.get("name"))
        return None

    def emails_who_can_view(self, account: dict, roles: list[str] | None = None) -> list[str]:
        """Users (listed explicitly in access.yaml) who may see this account —
        used to route alerts. Domain catch-alls are not expanded."""
        out = []
        for entry in self.cfg.get("users") or []:
            if roles and entry.get("role") not in roles:
                continue
            try:
                principal = self._principal(entry["email"], entry)
            except (AccessDenied, KeyError):
                continue
            if can_view(principal, account):
                out.append(principal.email)
        return out
