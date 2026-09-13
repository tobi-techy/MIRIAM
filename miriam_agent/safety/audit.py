import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from miriam_agent.database.models import AuditLog, Base

logger = logging.getLogger(__name__)


class AuditSystem:
    """Audit and compliance system for Miriam Financial Agent."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.async_session = None
        self.retention_days = 2555  # 7 years for financial compliance

    async def initialize(self):
        """Initialize the audit database connection."""
        # Create async engine
        self.engine = create_async_engine(self.database_url)

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create async session factory
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self):
        """Close audit database connections."""
        if self.engine:
            await self.engine.dispose()

    async def log_action(
        self,
        user_id: str,
        action: str,
        resource: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        risk_level: str | None = None,
    ) -> str:
        """Log an action for audit purposes."""
        try:
            async with self.async_session() as session:
                # Create audit log entry
                audit_log = AuditLog(
                    user_id=user_id,
                    action=action,
                    resource=resource,
                    resource_id=resource_id,
                    details=details or {},
                    created_at=datetime.utcnow(),
                )

                # Add risk level if provided
                if risk_level:
                    audit_log.details["risk_level"] = risk_level

                session.add(audit_log)
                await session.commit()

                logger.info(
                    "Action logged",
                    extra={
                        "user_id": user_id,
                        "action": action,
                        "resource": resource,
                        "audit_id": audit_log.id,
                    },
                )

                return audit_log.id

        except Exception:
            # Bug fix: this used to pass user_id/action as raw keyword
            # arguments to logger.error, which stdlib logging rejects
            # ("unexpected keyword argument"). That raised a *second*,
            # uncaught exception right here, which meant `log_action`
            # never actually completed successfully OR failed cleanly --
            # every call silently threw, so the audit trail was never
            # actually being written despite being invoked on every tool
            # execution.
            logger.error(
                "Error logging action",
                extra={"user_id": user_id, "action": action},
                exc_info=True,
            )
            raise

    async def log_money_movement(
        self,
        user_id: str,
        transaction_id: str,
        amount: float,
        currency: str,
        action: str,
        status: str,
        from_account: str | None = None,
        to_account: str | None = None,
        requires_approval: bool = False,
        approval_id: str | None = None,
    ) -> str:
        """Log a money movement transaction."""
        try:
            async with self.async_session() as session:
                # Create detailed log entry
                audit_log = AuditLog(
                    user_id=user_id,
                    action="money_movement",
                    resource="transaction",
                    resource_id=transaction_id,
                    details={
                        "transaction_id": transaction_id,
                        "amount": amount,
                        "currency": currency,
                        "action": action,
                        "status": status,
                        "from_account": from_account,
                        "to_account": to_account,
                        "requires_approval": requires_approval,
                        "approval_id": approval_id,
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                    created_at=datetime.utcnow(),
                )

                session.add(audit_log)
                await session.commit()

                logger.info(
                    "Money movement logged",
                    extra={
                        "user_id": user_id,
                        "transaction_id": transaction_id,
                        "amount": amount,
                        "currency": currency,
                        "status": status,
                        "audit_id": audit_log.id,
                    },
                )

                return audit_log.id

        except Exception:
            logger.error(
                "Error logging money movement",
                extra={"user_id": user_id, "transaction_id": transaction_id},
                exc_info=True,
            )
            raise

    async def get_user_audit_logs(
        self, user_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Get audit logs for a user."""
        try:
            async with self.async_session() as session:
                # Query audit logs
                query = (
                    select(AuditLog)
                    .where(AuditLog.user_id == user_id)
                    .order_by(AuditLog.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )

                logs = await session.execute(query)

                return [
                    {
                        "id": log.id,
                        "action": log.action,
                        "resource": log.resource,
                        "resource_id": log.resource_id,
                        "details": log.details,
                        "created_at": log.created_at.isoformat(),
                    }
                    for log in logs.scalars()
                ]

        except Exception:
            logger.error(
                "Error getting user audit logs",
                extra={"user_id": user_id},
                exc_info=True,
            )
            raise

    async def get_transaction_audit_logs(
        self, transaction_id: str
    ) -> list[dict[str, Any]]:
        """Get audit logs for a specific transaction."""
        try:
            async with self.async_session() as session:
                # Query audit logs for this transaction
                query = (
                    select(AuditLog)
                    .where(AuditLog.resource_id == transaction_id)
                    .where(AuditLog.resource == "transaction")
                    .order_by(AuditLog.created_at.desc())
                )

                logs = await session.execute(query)

                return [
                    {
                        "id": log.id,
                        "user_id": log.user_id,
                        "action": log.action,
                        "details": log.details,
                        "created_at": log.created_at.isoformat(),
                    }
                    for log in logs.scalars()
                ]

        except Exception:
            logger.error(
                "Error getting transaction audit logs",
                extra={"transaction_id": transaction_id},
                exc_info=True,
            )
            raise

    async def clean_old_logs(self, days_old: int = 365) -> int:
        """Clean up old audit logs for compliance."""
        try:
            async with self.async_session() as session:
                # Calculate cutoff date
                cutoff_date = datetime.utcnow() - timedelta(days=days_old)

                # Delete old logs
                query = select(AuditLog).where(AuditLog.created_at < cutoff_date)
                old_logs = await session.execute(query)

                # Count before deletion
                count = len(old_logs.scalars().all())

                # Delete
                await session.execute(query)
                await session.commit()

                logger.info(
                    "Old audit logs cleaned",
                    extra={"count": count, "cutoff_date": cutoff_date.isoformat()},
                )

                return count

        except Exception:
            logger.error(
                "Error cleaning old logs",
                exc_info=True,
            )
            raise

    async def export_audit_data(
        self, start_date: datetime, end_date: datetime, user_id: str | None = None
    ) -> dict[str, Any]:
        """Export audit data for compliance reporting."""
        try:
            async with self.async_session() as session:
                # Build query
                query = (
                    select(AuditLog)
                    .where(AuditLog.created_at >= start_date)
                    .where(AuditLog.created_at <= end_date)
                )

                if user_id:
                    query = query.where(AuditLog.user_id == user_id)

                # Execute query
                logs = await session.execute(query)

                # Format export data
                export_data = {
                    "export_date": datetime.utcnow().isoformat(),
                    "date_range": {
                        "start": start_date.isoformat(),
                        "end": end_date.isoformat(),
                    },
                    "logs": [],
                    "summary": {
                        "total_actions": 0,
                        "actions_by_type": {},
                        "users_affected": set(),
                    },
                }

                for log in logs.scalars():
                    log_data = {
                        "id": log.id,
                        "user_id": log.user_id,
                        "action": log.action,
                        "resource": log.resource,
                        "resource_id": log.resource_id,
                        "details": log.details,
                        "created_at": log.created_at.isoformat(),
                    }

                    export_data["logs"].append(log_data)

                    # Update summary
                    export_data["summary"]["total_actions"] += 1
                    export_data["summary"]["actions_by_type"][log.action] = (
                        export_data["summary"]["actions_by_type"].get(log.action, 0) + 1
                    )
                    export_data["summary"]["users_affected"].add(log.user_id)

                # Convert users_affected to list
                export_data["summary"]["users_affected"] = list(
                    export_data["summary"]["users_affected"]
                )

                return export_data

        except Exception:
            logger.error(
                "Error exporting audit data",
                exc_info=True,
            )
            raise

    async def check_compliance_violations(
        self, start_date: datetime, end_date: datetime
    ) -> list[dict[str, Any]]:
        """Check for compliance violations in audit logs."""
        try:
            async with self.async_session() as session:
                # Get logs in date range
                query = (
                    select(AuditLog)
                    .where(AuditLog.created_at >= start_date)
                    .where(AuditLog.created_at <= end_date)
                )

                logs = await session.execute(query)

                violations = []

                for log in logs.scalars():
                    # Check for potential violations
                    violation = await self._check_log_for_violations(log)
                    if violation:
                        violations.append(violation)

                return violations

        except Exception:
            logger.error(
                "Error checking compliance violations",
                exc_info=True,
            )
            raise

    async def _check_log_for_violations(self, log: AuditLog) -> dict[str, Any] | None:
        """Check a single log for potential violations."""
        try:
            # Check if log details contain suspicious information
            details = log.details

            # Check for patterns that might indicate violations
            if "amount" in details:
                amount = details["amount"]
                if amount > 10000:  # Large transaction
                    return {
                        "type": "large_transaction",
                        "severity": "medium",
                        "log_id": log.id,
                        "user_id": log.user_id,
                        "details": {
                            "amount": amount,
                            "action": log.action,
                            "resource": log.resource,
                        },
                    }

            if "status" in details and details["status"] == "failed":
                return {
                    "type": "failed_action",
                    "severity": "low",
                    "log_id": log.id,
                    "user_id": log.user_id,
                    "details": {
                        "action": log.action,
                        "resource": log.resource,
                        "status": details["status"],
                    },
                }

            # Add more violation checks as needed

            return None

        except Exception:
            logger.error(
                "Error checking log for violations",
                exc_info=True,
            )
            return None

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


_audit_singleton: AuditSystem | None = None


async def get_audit_system_singleton(database_url: str | None = None) -> AuditSystem:
    """Get the process-wide, lazily-initialized AuditSystem singleton.

    Used by SafetyPolicy to read real recent activity for a user. Unlike
    the request-scoped instance in api/dependencies.py, this one is shared
    across requests (audit rows are cheap to read repeatedly and the
    engine/session factory is safe to reuse).
    """
    global _audit_singleton
    if _audit_singleton is None:
        from miriam_agent.config.settings import get_settings

        url = database_url or get_settings().DATABASE_URL
        instance = AuditSystem(url)
        await instance.initialize()
        _audit_singleton = instance
    return _audit_singleton
