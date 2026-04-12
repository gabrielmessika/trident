from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from app.settings import AppConfig
from app.trident.pod_b.config_renderer import PassivbotConfigRenderer
from app.trident.pod_b.models import (
    PassivbotConfig,
    PassivbotFill,
    PassivbotInventory,
    PassivbotOrder,
    PassivbotPosition,
    PassivbotStatus,
)
from app.trident.pod_b.status_parser import PassivbotStatusParser
from app.trident.types import PodAllocation

logger = logging.getLogger(__name__)


class PassivbotManager:
    """Writes Pod B runtime config and tracks a lightweight local status."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.renderer = PassivbotConfigRenderer()
        self.status_parser = PassivbotStatusParser()
        self._managed_processes: dict[int, subprocess.Popen[bytes]] = {}

    def sync(
        self,
        *,
        allocation: PodAllocation,
        owned_symbols: list[str],
        managed_symbols: list[str] | None = None,
    ) -> PassivbotStatus:
        config_path = Path(self.config.pod_b.passivbot_config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        managed_scope_symbols = [symbol.upper() for symbol in (managed_symbols or owned_symbols)]
        opening_symbols = [symbol.upper() for symbol in owned_symbols]
        unwind_only_symbols = [
            symbol for symbol in managed_scope_symbols if symbol not in set(opening_symbols)
        ]

        runtime_config = PassivbotConfig(
            config_path=str(config_path),
            approved_coins=managed_scope_symbols,
            target_pct=allocation.target_pct,
            target_usd=allocation.target_usd,
            metadata={
                "opening_symbols": opening_symbols,
                "unwind_only_symbols": unwind_only_symbols,
                "capped_by_pod_limit": allocation.capped_by_pod_limit,
                "paper_quote_width_bps": self.config.pod_b.paper_quote_width_bps,
                "paper_order_size_pct": self.config.pod_b.paper_order_size_pct,
                "paper_max_inventory_skew_pct": self.config.pod_b.paper_max_inventory_skew_pct,
                "paper_maker_fee_bps": self.config.pod_b.paper_maker_fee_bps,
                "paper_recent_fills_limit": self.config.pod_b.paper_recent_fills_limit,
                "paper_pause_outside_range": self.config.pod_b.paper_pause_outside_range,
                "paper_guard_max_adx": self.config.pod_b.paper_guard_max_adx,
                "paper_guard_max_atr_ratio": self.config.pod_b.paper_guard_max_atr_ratio,
                "paper_guard_max_abs_structure_score": self.config.pod_b.paper_guard_max_abs_structure_score,
                "paper_guard_max_range_width_bps": self.config.pod_b.paper_guard_max_range_width_bps,
                "paper_flow_toxicity_threshold": self.config.pod_b.paper_flow_toxicity_threshold,
                "paper_one_sided_inventory_threshold_pct": self.config.pod_b.paper_one_sided_inventory_threshold_pct,
                "paper_quote_width_bucket_multiplier": self.config.pod_b.paper_quote_width_bucket_multiplier,
                "paper_quote_width_toxicity_multiplier": self.config.pod_b.paper_quote_width_toxicity_multiplier,
                "paper_order_size_toxicity_discount": self.config.pod_b.paper_order_size_toxicity_discount,
                "paper_quote_width_multiplier_by_symbol": self.config.pod_b.paper_quote_width_multiplier_by_symbol,
                "paper_order_size_multiplier_by_symbol": self.config.pod_b.paper_order_size_multiplier_by_symbol,
                "paper_max_inventory_skew_pct_by_symbol": self.config.pod_b.paper_max_inventory_skew_pct_by_symbol,
            },
        )
        rendered = self.renderer.render(runtime_config)
        config_path.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")

        status = self.status_parser.parse(
            enabled=self.config.pod_b.enabled,
            config_path=str(config_path),
            status_path=str(self.status_path(config_path)),
            target_usd=allocation.target_usd,
            managed_symbols=managed_scope_symbols,
            default_reason=self._default_reason(allocation, managed_scope_symbols),
        )
        return self._normalize_status(
            status=status,
            managed_symbols=managed_scope_symbols,
            target_usd=allocation.target_usd,
        )

    def read_status(
        self,
        *,
        allocation: PodAllocation,
        owned_symbols: list[str],
        managed_symbols: list[str] | None = None,
    ) -> PassivbotStatus:
        config_path = Path(self.config.pod_b.passivbot_config_path)
        managed_scope_symbols = [symbol.upper() for symbol in (managed_symbols or owned_symbols)]
        status = self.status_parser.parse(
            enabled=self.config.pod_b.enabled,
            config_path=str(config_path),
            status_path=str(self.status_path(config_path)),
            target_usd=allocation.target_usd,
            managed_symbols=managed_scope_symbols,
            default_reason=self._default_reason(allocation, managed_scope_symbols),
        )
        return self._normalize_status(
            status=status,
            managed_symbols=managed_scope_symbols,
            target_usd=allocation.target_usd,
        )

    def status_path(self, config_path: str | Path | None = None) -> Path:
        path = Path(config_path or self.config.pod_b.passivbot_config_path)
        return path.with_suffix(".status.json")

    def stdout_path(self, config_path: str | Path | None = None) -> Path:
        path = Path(config_path or self.config.pod_b.passivbot_config_path)
        return path.with_suffix(".stdout.log")

    def stderr_path(self, config_path: str | Path | None = None) -> Path:
        path = Path(config_path or self.config.pod_b.passivbot_config_path)
        return path.with_suffix(".stderr.log")

    def build_launch_command(self) -> list[str]:
        if self.config.pod_b.launch_command:
            return [
                part.format(
                    config=self.config.pod_b.passivbot_config_path,
                    workdir=self.config.pod_b.launch_workdir,
                )
                for part in self.config.pod_b.launch_command
            ]
        return ["passivbot", "live", self.config.pod_b.passivbot_config_path]

    def start(
        self,
        *,
        allocation: PodAllocation,
        owned_symbols: list[str],
        command: list[str] | None = None,
    ) -> PassivbotStatus:
        status = self.sync(allocation=allocation, owned_symbols=owned_symbols)
        if status.last_sync_reason in {
            "pod_disabled",
            "no_symbols_assigned",
            "zero_target_allocation",
        }:
            return status
        if status.pid is not None and self._pid_is_active(status.pid):
            return status

        launch_command = command or self.build_launch_command()
        stdout_path = self.stdout_path(status.config_path)
        stderr_path = self.stderr_path(status.config_path)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)

        with stdout_path.open("ab") as stdout_handle, stderr_path.open(
            "ab"
        ) as stderr_handle:
            process = subprocess.Popen(
                launch_command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=(
                    self.config.pod_b.launch_workdir
                    or str(Path(status.config_path).parent)
                ),
            )
        self._managed_processes[process.pid] = process

        return self._write_status(
            enabled=status.enabled,
            process_state="running",
            managed_symbols=owned_symbols,
            config_path=status.config_path,
            target_usd=allocation.target_usd,
            last_sync_reason="started_by_trident",
            leverage=status.leverage,
            pid=process.pid,
            launch_command=launch_command,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            started_at=self._utc_now(),
            positions=status.positions,
            open_orders=status.open_orders,
            inventory=status.inventory,
            recent_fills=status.recent_fills,
            total_fill_count=status.total_fill_count,
            total_position_count=status.total_position_count,
            total_open_order_count=status.total_open_order_count,
            realized_pnl_usd=status.realized_pnl_usd,
            total_notional_usd=status.total_notional_usd,
            total_unrealized_pnl_usd=status.total_unrealized_pnl_usd,
        )

    def stop(self) -> PassivbotStatus:
        config_path = Path(self.config.pod_b.passivbot_config_path)
        managed_symbols = self._managed_symbols_from_runtime(config_path)
        status = self.status_parser.parse(
            enabled=self.config.pod_b.enabled,
            config_path=str(config_path),
            status_path=str(self.status_path(config_path)),
            target_usd=0.0,
            managed_symbols=managed_symbols,
            default_reason="status_probe",
        )
        if status.pid is not None and self._pid_is_active(status.pid):
            process = self._managed_processes.get(status.pid)
            try:
                os.kill(status.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not self._pid_is_active(status.pid):
                    break
                time.sleep(0.05)
            if self._pid_is_active(status.pid):
                try:
                    os.kill(status.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process is not None:
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
                finally:
                    self._managed_processes.pop(status.pid, None)
        return self._write_status(
            enabled=self.config.pod_b.enabled,
            process_state="stopped",
            managed_symbols=status.managed_symbols,
            config_path=status.config_path,
            target_usd=status.target_usd,
            last_sync_reason="stopped_by_trident",
            leverage=status.leverage,
            pid=None,
            launch_command=status.launch_command,
            stdout_path=status.stdout_path,
            stderr_path=status.stderr_path,
            started_at=status.started_at,
            positions=status.positions,
            open_orders=status.open_orders,
            inventory=status.inventory,
            recent_fills=status.recent_fills,
            total_fill_count=status.total_fill_count,
            total_position_count=status.total_position_count,
            total_open_order_count=status.total_open_order_count,
            realized_pnl_usd=status.realized_pnl_usd,
            total_notional_usd=status.total_notional_usd,
            total_unrealized_pnl_usd=status.total_unrealized_pnl_usd,
        )

    def restart(
        self,
        *,
        allocation: PodAllocation,
        owned_symbols: list[str],
        command: list[str] | None = None,
    ) -> PassivbotStatus:
        self.stop()
        return self.start(
            allocation=allocation,
            owned_symbols=owned_symbols,
            command=command,
        )

    def _default_reason(self, allocation: PodAllocation, owned_symbols: list[str]) -> str:
        if not self.config.pod_b.enabled:
            return "pod_disabled"
        if not owned_symbols:
            return "no_symbols_assigned"
        if allocation.target_usd <= 0:
            return "zero_target_allocation"
        return "config_rendered"

    def _managed_symbols_from_runtime(self, config_path: Path) -> list[str]:
        if not config_path.exists():
            return []
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        trident = payload.get("trident", {})
        if not isinstance(trident, dict):
            return []
        managed_symbols = trident.get("managed_symbols", [])
        if not isinstance(managed_symbols, list):
            return []
        return [str(symbol).upper() for symbol in managed_symbols]

    def _normalize_status(
        self,
        *,
        status: PassivbotStatus,
        managed_symbols: list[str],
        target_usd: float,
    ) -> PassivbotStatus:
        status = self._trim_status_to_assignment(
            status=status,
            managed_symbols=managed_symbols,
        )
        if status.pid is None:
            return status
        if self._pid_is_active(status.pid):
            return status
        process = self._managed_processes.pop(status.pid, None)
        if process is not None:
            process.wait(timeout=0.1)
        inventory = self.status_parser._build_inventory_defaults(
            managed_symbols=managed_symbols,
            target_usd=target_usd,
            positions=status.positions,
            open_orders=status.open_orders,
        )
        return self._write_status(
            enabled=status.enabled,
            process_state="stopped",
            managed_symbols=managed_symbols,
            config_path=status.config_path,
            target_usd=target_usd,
            last_sync_reason="process_exited",
            leverage=status.leverage,
            pid=None,
            launch_command=status.launch_command,
            stdout_path=status.stdout_path,
            stderr_path=status.stderr_path,
            started_at=status.started_at,
            positions=status.positions,
            open_orders=status.open_orders,
            inventory=inventory,
            total_position_count=status.total_position_count,
            total_open_order_count=status.total_open_order_count,
            total_notional_usd=status.total_notional_usd,
            total_unrealized_pnl_usd=status.total_unrealized_pnl_usd,
        )

    def _trim_status_to_assignment(
        self,
        *,
        status: PassivbotStatus,
        managed_symbols: list[str],
    ) -> PassivbotStatus:
        desired_symbols = [symbol.upper() for symbol in managed_symbols]
        runtime_symbols = {
            str(symbol).upper()
            for symbol in status.managed_symbols
            if str(symbol).strip()
        }
        stale_symbols = sorted(runtime_symbols - set(desired_symbols))
        missing_symbols = sorted(set(desired_symbols) - runtime_symbols)
        effective_symbols = [
            symbol for symbol in desired_symbols if symbol in runtime_symbols
        ]
        if not status.managed_symbols:
            effective_symbols = desired_symbols
        allowed_symbols = set(effective_symbols)

        filtered_positions = [
            position
            for position in status.positions
            if position.symbol.upper() in allowed_symbols
        ]
        filtered_open_orders = [
            order
            for order in status.open_orders
            if order.symbol.upper() in allowed_symbols
        ]
        filtered_recent_fills = [
            fill
            for fill in status.recent_fills
            if fill.symbol.upper() in allowed_symbols
        ]
        dropped_order_symbols = sorted(
            {
                order.symbol.upper()
                for order in status.open_orders
                if order.symbol.upper() not in allowed_symbols
            }
        )
        dropped_position_symbols = sorted(
            {
                position.symbol.upper()
                for position in status.positions
                if position.symbol.upper() not in allowed_symbols
            }
        )
        inventory = self.status_parser._build_inventory_defaults(
            managed_symbols=effective_symbols,
            target_usd=status.target_usd,
            positions=filtered_positions,
            open_orders=filtered_open_orders,
        )

        changed = (
            status.managed_symbols != effective_symbols
            or len(filtered_positions) != len(status.positions)
            or len(filtered_open_orders) != len(status.open_orders)
            or len(filtered_recent_fills) != len(status.recent_fills)
            or len(inventory) != len(status.inventory)
            or any(
                existing.symbol != rebuilt.symbol
                or existing.target_notional_usd != rebuilt.target_notional_usd
                or existing.current_notional_usd != rebuilt.current_notional_usd
                or existing.inventory_skew_pct != rebuilt.inventory_skew_pct
                or existing.has_position != rebuilt.has_position
                or existing.open_order_count != rebuilt.open_order_count
                for existing, rebuilt in zip(status.inventory, inventory)
            )
        )
        if stale_symbols or missing_symbols:
            if status.process_state == "running" or status.pid is not None:
                logger.warning(
                    "Pod B runtime symbol drift detected; desired=%s runtime=%s effective=%s stale=%s missing=%s process_state=%s reason=%s pid=%s",
                    desired_symbols,
                    status.managed_symbols,
                    effective_symbols,
                    stale_symbols,
                    missing_symbols,
                    status.process_state,
                    status.last_sync_reason,
                    status.pid,
                )
            else:
                logger.debug(
                    "Pod B runtime symbol drift ignored for inactive runtime; desired=%s runtime=%s effective=%s stale=%s missing=%s process_state=%s reason=%s pid=%s",
                    desired_symbols,
                    status.managed_symbols,
                    effective_symbols,
                    stale_symbols,
                    missing_symbols,
                    status.process_state,
                    status.last_sync_reason,
                    status.pid,
                )
        if dropped_order_symbols or dropped_position_symbols:
            if status.process_state == "running" or status.pid is not None:
                logger.warning(
                    "Pod B trimmed stale runtime artifacts; dropped_order_symbols=%s dropped_position_symbols=%s total_open_orders=%s total_positions=%s",
                    dropped_order_symbols,
                    dropped_position_symbols,
                    len(status.open_orders) - len(filtered_open_orders),
                    len(status.positions) - len(filtered_positions),
                )
            else:
                logger.debug(
                    "Pod B trimmed stale runtime artifacts for inactive runtime; dropped_order_symbols=%s dropped_position_symbols=%s total_open_orders=%s total_positions=%s",
                    dropped_order_symbols,
                    dropped_position_symbols,
                    len(status.open_orders) - len(filtered_open_orders),
                    len(status.positions) - len(filtered_positions),
                )
        if not changed:
            return status

        return self._write_status(
            enabled=status.enabled,
            process_state=status.process_state,
            managed_symbols=effective_symbols,
            config_path=status.config_path,
            target_usd=status.target_usd,
            last_sync_reason=status.last_sync_reason,
            leverage=status.leverage,
            pid=status.pid,
            launch_command=status.launch_command,
            stdout_path=status.stdout_path,
            stderr_path=status.stderr_path,
            started_at=status.started_at,
            positions=filtered_positions,
            open_orders=filtered_open_orders,
            inventory=inventory,
            recent_fills=filtered_recent_fills,
            total_fill_count=status.total_fill_count,
            total_position_count=len(filtered_positions),
            total_open_order_count=len(filtered_open_orders),
            realized_pnl_usd=status.realized_pnl_usd,
            total_notional_usd=round(
                sum(position.notional_usd for position in filtered_positions),
                4,
            ),
            total_unrealized_pnl_usd=round(
                sum(position.unrealized_pnl_usd for position in filtered_positions),
                4,
            ),
        )

    def _write_status(
        self,
        *,
        enabled: bool,
        process_state: str,
        managed_symbols: list[str],
        config_path: str,
        target_usd: float,
        last_sync_reason: str,
        leverage: float | None,
        pid: int | None,
        launch_command: list[str],
        stdout_path: str,
        stderr_path: str,
        started_at: str | None,
        positions: list[PassivbotPosition] | None = None,
        open_orders: list[PassivbotOrder] | None = None,
        inventory: list[PassivbotInventory] | None = None,
        recent_fills: list[PassivbotFill] | None = None,
        total_fill_count: int | None = None,
        total_position_count: int | None = None,
        total_open_order_count: int | None = None,
        realized_pnl_usd: float | None = None,
        total_notional_usd: float | None = None,
        total_unrealized_pnl_usd: float | None = None,
    ) -> PassivbotStatus:
        parsed_positions = positions or []
        parsed_open_orders = open_orders or []
        parsed_inventory = inventory or []
        parsed_recent_fills = recent_fills or []
        status = PassivbotStatus(
            enabled=enabled,
            process_state=process_state,
            managed_symbols=managed_symbols,
            config_path=config_path,
            status_path=str(self.status_path(config_path)),
            target_usd=target_usd,
            last_sync_reason=last_sync_reason,
            leverage=leverage,
            updated_at=self._utc_now(),
            pid=pid,
            launch_command=launch_command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            positions=parsed_positions,
            open_orders=parsed_open_orders,
            inventory=parsed_inventory,
            recent_fills=parsed_recent_fills,
            total_fill_count=(
                total_fill_count
                if total_fill_count is not None
                else len(parsed_recent_fills)
            ),
            total_position_count=(
                total_position_count
                if total_position_count is not None
                else len(parsed_positions)
            ),
            total_open_order_count=(
                total_open_order_count
                if total_open_order_count is not None
                else len(parsed_open_orders)
            ),
            realized_pnl_usd=(
                round(realized_pnl_usd, 4) if realized_pnl_usd is not None else 0.0
            ),
            total_notional_usd=(
                round(total_notional_usd, 4)
                if total_notional_usd is not None
                else round(sum(position.notional_usd for position in parsed_positions), 4)
            ),
            total_unrealized_pnl_usd=(
                round(total_unrealized_pnl_usd, 4)
                if total_unrealized_pnl_usd is not None
                else round(
                    sum(position.unrealized_pnl_usd for position in parsed_positions),
                    4,
                )
            ),
        )
        status_path = Path(status.status_path)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(status.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        return status

    def _pid_is_active(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
