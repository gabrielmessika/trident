#!/usr/bin/env python3
"""Disk retention for TRIDENT server data.

The policy is intentionally conservative:
- never delete runtime state, status files, configs, trades, or settlements;
- prune old daily snapshot/feature files that are reproducible/fetched data;
- rotate very large observation-only HIP-4 logs into compressed archives;
- record every action in logs/retention_runs.jsonl for later review.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class RetentionAction:
    action: str
    path: str
    bytes_before: int = 0
    bytes_after: int = 0
    detail: str = ""

    @property
    def bytes_reclaimed(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "path": self.path,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_reclaimed": self.bytes_reclaimed,
            "detail": self.detail,
        }


class RetentionRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.now = utc_now()
        self.actions: list[RetentionAction] = []
        self.errors: list[str] = []

    def run(self) -> int:
        if self.args.scope in {"all", "trident"}:
            self._prune_trident(Path(self.args.trident_root))
        if self.args.scope in {"all", "hip4"}:
            self._prune_hip4(Path(self.args.hip4_root))
        self._write_manifest()
        self._print_summary()
        return 1 if self.errors and self.args.fail_on_error else 0

    def _prune_trident(self, root: Path) -> None:
        if not root.exists():
            self._note("skip_missing_root", root, detail="trident root missing")
            return
        self._delete_old_files(
            root / "data" / "live_snapshots",
            ["*.jsonl", "*.jsonl.bak-*"],
            self.args.snapshot_days,
            "old_trident_snapshots",
        )
        self._delete_old_files(
            root / "data" / "live_snapshots_testnet",
            ["*.jsonl", "*.jsonl.bak-*"],
            self.args.snapshot_days,
            "old_trident_testnet_snapshots",
        )
        self._delete_old_files(
            root / "data" / "live_features",
            ["*.jsonl", "*.csv"],
            self.args.feature_days,
            "old_trident_live_features",
            recursive=True,
        )
        self._delete_old_files(
            root / "data" / "live_features_testnet",
            ["*.jsonl", "*.csv"],
            self.args.feature_days,
            "old_trident_testnet_live_features",
            recursive=True,
        )
        self._delete_old_dirs(
            root / "logs" / "runtime_status_backups",
            ["*"],
            self.args.runtime_backup_days,
            "old_runtime_status_backup",
        )
        self._delete_old_dirs(
            root / "logs" / "archive",
            ["*"],
            self.args.runtime_backup_days,
            "old_trident_log_archive",
        )
        self._delete_old_files(
            root / "logs",
            ["retention_runs.jsonl"],
            self.args.manifest_days,
            "old_retention_manifest",
        )

    def _prune_hip4(self, root: Path) -> None:
        if not root.exists():
            self._note("skip_missing_root", root, detail="hip4 root missing")
            return
        self._delete_old_dirs(
            root,
            ["legacy-from-*"],
            self.args.legacy_days,
            "old_hip4_legacy_archive",
        )
        self._delete_old_dirs(
            root / "logs" / "retention_archive",
            ["*"],
            self.args.archive_days,
            "old_hip4_retention_archive",
        )
        self._delete_old_files(
            root / "logs",
            ["retention_runs.jsonl"],
            self.args.manifest_days,
            "old_retention_manifest",
        )
        self._rotate_hip4_logs(root)

    def _rotate_hip4_logs(self, root: Path) -> None:
        profile_thresholds = {
            "hip4_outcome_mainnet": {
                "market_observations.jsonl": self.args.hip4_market_max_mb,
                "decisions.jsonl": self.args.hip4_decision_max_mb,
            },
            "hip4_outcome_mainnet_paper": {
                "market_observations.jsonl": self.args.hip4_market_max_mb,
                "decisions.jsonl": self.args.hip4_decision_max_mb,
            },
            "hip4_outcome_testnet": {
                "market_observations.jsonl": self.args.hip4_market_max_mb,
                "decisions.jsonl": self.args.hip4_decision_max_mb,
            },
            "hip4_outcome_paper": {
                "market_observations.jsonl": self.args.hip4_market_max_mb,
                "decisions.jsonl": self.args.hip4_decision_max_mb,
            },
            "hip4_nautilus_shadow": {
                "book_snapshots.jsonl": self.args.hip4_shadow_max_mb,
                "instruments.jsonl": self.args.hip4_shadow_max_mb,
                "parity_compare.csv": self.args.hip4_shadow_max_mb,
                "data_quality.csv": self.args.hip4_shadow_max_mb,
            },
        }
        for profile, thresholds in profile_thresholds.items():
            profile_dir = root / "logs" / profile
            for file_name, max_mb in thresholds.items():
                self._rotate_file_if_large(
                    profile_dir / file_name,
                    max_bytes=int(max_mb * 1024 * 1024),
                    archive_root=root / "logs" / "retention_archive",
                    profile=profile,
                )

    def _delete_old_files(
        self,
        directory: Path,
        patterns: Iterable[str],
        days: int,
        reason: str,
        *,
        recursive: bool = False,
    ) -> None:
        if days < 0:
            return
        if not directory.exists():
            return
        cutoff = self.now.timestamp() - (days * 86400)
        for path in self._iter_matches(directory, patterns, recursive=recursive):
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            self._delete_file(path, reason)

    def _delete_old_dirs(
        self,
        directory: Path,
        patterns: Iterable[str],
        days: int,
        reason: str,
    ) -> None:
        if days < 0:
            return
        if not directory.exists():
            return
        cutoff = self.now.timestamp() - (days * 86400)
        for path in self._iter_matches(directory, patterns, recursive=False):
            if not path.is_dir() or path.stat().st_mtime >= cutoff:
                continue
            self._delete_dir(path, reason)

    def _rotate_file_if_large(
        self,
        path: Path,
        *,
        max_bytes: int,
        archive_root: Path,
        profile: str,
    ) -> None:
        if max_bytes <= 0 or not path.exists() or not path.is_file():
            return
        size = path.stat().st_size
        if size < max_bytes:
            return
        archive_dir = archive_root / utc_stamp(self.now) / profile
        archived_plain = archive_dir / path.name
        archived_gz = archived_plain.with_suffix(archived_plain.suffix + ".gz")
        detail = f"rotate_large_file max_bytes={max_bytes}"
        if not self.args.apply:
            self._note("would_rotate_gzip", path, bytes_before=size, detail=detail)
            return
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            path.replace(archived_plain)
            if path.suffix == ".jsonl":
                path.touch(mode=0o644, exist_ok=True)
            with archived_plain.open("rb") as src, gzip.open(archived_gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            compressed_size = archived_gz.stat().st_size
            archived_plain.unlink()
            self.actions.append(
                RetentionAction(
                    action="rotate_gzip",
                    path=str(path),
                    bytes_before=size,
                    bytes_after=compressed_size,
                    detail=f"{detail} archive={archived_gz}",
                )
            )
        except Exception as exc:  # pragma: no cover - operational guard
            self._error(f"rotate_failed path={path} error={exc}")

    def _delete_file(self, path: Path, reason: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if not self.args.apply:
            self._note("would_delete_file", path, bytes_before=size, detail=reason)
            return
        try:
            path.unlink()
            self.actions.append(
                RetentionAction(
                    action="delete_file",
                    path=str(path),
                    bytes_before=size,
                    detail=reason,
                )
            )
        except Exception as exc:  # pragma: no cover - operational guard
            self._error(f"delete_file_failed path={path} error={exc}")

    def _delete_dir(self, path: Path, reason: str) -> None:
        size = self._tree_size(path)
        if not self.args.apply:
            self._note("would_delete_dir", path, bytes_before=size, detail=reason)
            return
        try:
            shutil.rmtree(path)
            self.actions.append(
                RetentionAction(
                    action="delete_dir",
                    path=str(path),
                    bytes_before=size,
                    detail=reason,
                )
            )
        except Exception as exc:  # pragma: no cover - operational guard
            self._error(f"delete_dir_failed path={path} error={exc}")

    def _note(
        self,
        action: str,
        path: Path,
        *,
        bytes_before: int = 0,
        detail: str = "",
    ) -> None:
        self.actions.append(
            RetentionAction(
                action=action,
                path=str(path),
                bytes_before=bytes_before,
                detail=detail,
            )
        )

    def _error(self, message: str) -> None:
        self.errors.append(message)
        print(f"[WARN] {message}", file=sys.stderr)

    def _iter_matches(
        self,
        directory: Path,
        patterns: Iterable[str],
        *,
        recursive: bool,
    ) -> Iterable[Path]:
        for pattern in patterns:
            yield from (directory.rglob(pattern) if recursive else directory.glob(pattern))

    def _tree_size(self, path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total

    def _write_manifest(self) -> None:
        if not self.args.apply:
            return
        manifest_paths = []
        if self.args.scope in {"all", "trident"}:
            manifest_paths.append(Path(self.args.trident_root) / "logs" / "retention_runs.jsonl")
        if self.args.scope in {"all", "hip4"}:
            manifest_paths.append(Path(self.args.hip4_root) / "logs" / "retention_runs.jsonl")

        payload = {
            "generated_at": self.now.isoformat().replace("+00:00", "Z"),
            "scope": self.args.scope,
            "apply": bool(self.args.apply),
            "retention": {
                "snapshot_days": self.args.snapshot_days,
                "feature_days": self.args.feature_days,
                "runtime_backup_days": self.args.runtime_backup_days,
                "archive_days": self.args.archive_days,
                "legacy_days": self.args.legacy_days,
                "hip4_market_max_mb": self.args.hip4_market_max_mb,
                "hip4_decision_max_mb": self.args.hip4_decision_max_mb,
                "hip4_shadow_max_mb": self.args.hip4_shadow_max_mb,
            },
            "bytes_reclaimed": sum(action.bytes_reclaimed for action in self.actions),
            "actions": [action.to_dict() for action in self.actions],
            "errors": list(self.errors),
        }
        for manifest_path in manifest_paths:
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                with manifest_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, sort_keys=True) + "\n")
            except Exception as exc:  # pragma: no cover - operational guard
                self._error(f"manifest_write_failed path={manifest_path} error={exc}")

    def _print_summary(self) -> None:
        reclaimed = sum(action.bytes_reclaimed for action in self.actions)
        mode = "apply" if self.args.apply else "dry-run"
        print(
            json.dumps(
                {
                    "mode": mode,
                    "scope": self.args.scope,
                    "action_count": len(self.actions),
                    "bytes_reclaimed": reclaimed,
                    "errors": self.errors,
                },
                sort_keys=True,
            )
        )
        for action in self.actions[: self.args.print_limit]:
            print(json.dumps(action.to_dict(), sort_keys=True))
        remaining = len(self.actions) - self.args.print_limit
        if remaining > 0:
            print(json.dumps({"remaining_actions_not_printed": remaining}, sort_keys=True))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply TRIDENT server disk retention")
    parser.add_argument("--scope", choices=["all", "trident", "hip4"], default=os.getenv("TRIDENT_RETENTION_SCOPE", "all"))
    parser.add_argument("--trident-root", default=os.getenv("TRIDENT_RETENTION_TRIDENT_ROOT", "/opt/trident"))
    parser.add_argument("--hip4-root", default=os.getenv("TRIDENT_RETENTION_HIP4_ROOT", "/opt/trident-hip4"))
    parser.add_argument("--apply", action="store_true", help="Delete/rotate files. Default is dry-run.")
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--snapshot-days", type=int, default=env_int("TRIDENT_RETENTION_SNAPSHOT_DAYS", 21))
    parser.add_argument("--feature-days", type=int, default=env_int("TRIDENT_RETENTION_FEATURE_DAYS", 14))
    parser.add_argument(
        "--runtime-backup-days",
        type=int,
        default=env_int("TRIDENT_RETENTION_RUNTIME_BACKUP_DAYS", 30),
    )
    parser.add_argument("--archive-days", type=int, default=env_int("TRIDENT_RETENTION_ARCHIVE_DAYS", 21))
    parser.add_argument("--legacy-days", type=int, default=env_int("TRIDENT_RETENTION_LEGACY_DAYS", 14))
    parser.add_argument("--manifest-days", type=int, default=env_int("TRIDENT_RETENTION_MANIFEST_DAYS", 120))
    parser.add_argument(
        "--hip4-market-max-mb",
        type=float,
        default=env_float("TRIDENT_RETENTION_HIP4_MARKET_MAX_MB", 2048.0),
    )
    parser.add_argument(
        "--hip4-decision-max-mb",
        type=float,
        default=env_float("TRIDENT_RETENTION_HIP4_DECISION_MAX_MB", 2048.0),
    )
    parser.add_argument(
        "--hip4-shadow-max-mb",
        type=float,
        default=env_float("TRIDENT_RETENTION_HIP4_SHADOW_MAX_MB", 1024.0),
    )
    parser.add_argument("--print-limit", type=int, default=env_int("TRIDENT_RETENTION_PRINT_LIMIT", 80))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    return RetentionRunner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
