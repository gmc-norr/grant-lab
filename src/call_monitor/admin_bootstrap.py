from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import User


def bootstrap_admins(
    session_factory: sessionmaker, emails: Iterable[str]
) -> None:
    normalized = [e.strip().lower() for e in emails if e and e.strip()]
    if not normalized:
        return
    with session_factory() as session:
        for email in normalized:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                session.add(User(email=email, is_admin=True))
            elif not user.is_admin:
                user.is_admin = True
        session.commit()
