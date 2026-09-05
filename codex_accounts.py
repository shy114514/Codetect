#!/usr/bin/env python3
"""Interactively save and switch Codex authentication profiles."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path


class AccountError(Exception):
    """Raised when an account operation cannot be completed safely."""


@dataclass(frozen=True)
class Paths:
    codex_dir: Path

    @property
    def accounts_dir(self) -> Path:
        return self.codex_dir / "accounts"

    @property
    def profiles_dir(self) -> Path:
        return self.accounts_dir / "profiles"

    @property
    def metadata(self) -> Path:
        return self.accounts_dir / "metadata.json"

    @property
    def active_auth(self) -> Path:
        return self.codex_dir / "auth.json"

    @property
    def active_config(self) -> Path:
        return self.codex_dir / "config.toml"

    def profile_dir(self, account_id: str) -> Path:
        return self.profiles_dir / account_id

    def profile_auth(self, account_id: str) -> Path:
        return self.profile_dir(account_id) / "auth.json"

    def profile_config(self, account_id: str) -> Path:
        return self.profile_dir(account_id) / "config.toml"


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise AccountError("Account name cannot be empty.")
    if len(name) > 80 or any(ord(char) < 32 for char in name):
        raise AccountError("Account name must be at most 80 printable characters.")
    return name


def _validate_auth(content: bytes, path: Path) -> None:
    try:
        json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccountError(f"Invalid JSON in {path}: {error}") from error


def _validate_config(content: bytes, path: Path) -> None:
    try:
        tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AccountError(f"Invalid TOML in {path}: {error}") from error


def _write_atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
        path.chmod(mode)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


class AccountManager:
    def __init__(self, paths: Paths):
        self.paths = paths

    def _ensure_storage(self) -> None:
        self.paths.accounts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths.accounts_dir.chmod(0o700)
        self.paths.profiles_dir.mkdir(mode=0o700, exist_ok=True)
        self.paths.profiles_dir.chmod(0o700)

    def _load_metadata(self) -> dict:
        if not self.paths.metadata.exists():
            return {"version": 1, "current_account_id": None, "accounts": []}
        try:
            data = json.loads(self.paths.metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AccountError(f"Cannot read account metadata: {error}") from error
        if data.get("version") != 1 or not isinstance(data.get("accounts"), list):
            raise AccountError("Account metadata has an unsupported format.")
        return data

    def _save_metadata(self, data: dict) -> None:
        self._ensure_storage()
        _write_atomic(
            self.paths.metadata,
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def accounts(self) -> list[dict]:
        return self._load_metadata()["accounts"]

    def current_account(self) -> dict | None:
        data = self._load_metadata()
        current_id = data["current_account_id"]
        return next((item for item in data["accounts"] if item["id"] == current_id), None)

    def _account_by_id(self, account_id: str) -> dict:
        account = next((item for item in self.accounts() if item["id"] == account_id), None)
        if account is None:
            raise AccountError("Selected account no longer exists.")
        return account

    def _read_active(self) -> tuple[bytes, bytes]:
        if not self.paths.active_auth.is_file() or not self.paths.active_config.is_file():
            raise AccountError(
                f"Codex files are required: {self.paths.active_auth} and {self.paths.active_config}"
            )
        auth = self.paths.active_auth.read_bytes()
        config = self.paths.active_config.read_bytes()
        _validate_auth(auth, self.paths.active_auth)
        _validate_config(config, self.paths.active_config)
        return auth, config

    def detect_auth_type(self) -> str:
        auth, _ = self._read_active()
        data = json.loads(auth.decode("utf-8"))
        return "chatgpt" if data.get("auth_mode") == "chatgpt" else "api_key"

    def save_current(self, name: str, auth_type: str, account_id: str | None = None) -> dict:
        name = _validate_name(name)
        if auth_type not in {"chatgpt", "api_key"}:
            raise AccountError("Authentication type must be chatgpt or api_key.")
        auth, config = self._read_active()
        data = self._load_metadata()

        if account_id is None:
            if any(item["name"] == name for item in data["accounts"]):
                raise AccountError(f"An account named {name!r} already exists.")
            account = {"id": uuid.uuid4().hex, "name": name, "auth_type": auth_type}
            data["accounts"].append(account)
        else:
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if account is None:
                raise AccountError("Selected account no longer exists.")
            account["name"] = name
            account["auth_type"] = auth_type

        profile_dir = self.paths.profile_dir(account["id"])
        profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_dir.chmod(0o700)
        _write_atomic(self.paths.profile_auth(account["id"]), auth)
        _write_atomic(self.paths.profile_config(account["id"]), config)
        data["current_account_id"] = account["id"]
        self._save_metadata(data)
        return account

    def switch_to(self, account_id: str) -> dict:
        account = self._account_by_id(account_id)
        auth_path = self.paths.profile_auth(account_id)
        config_path = self.paths.profile_config(account_id)
        if not auth_path.is_file() or not config_path.is_file():
            raise AccountError(f"Snapshot for {account['name']!r} is incomplete.")
        target_auth = auth_path.read_bytes()
        target_config = config_path.read_bytes()
        _validate_auth(target_auth, auth_path)
        _validate_config(target_config, config_path)
        old_auth, old_config = self._read_active()
        previous_metadata = self._load_metadata()

        try:
            _write_atomic(self.paths.active_auth, target_auth)
            _write_atomic(self.paths.active_config, target_config)
            updated_metadata = self._load_metadata()
            updated_metadata["current_account_id"] = account_id
            self._save_metadata(updated_metadata)
        except Exception as error:
            try:
                _write_atomic(self.paths.active_auth, old_auth)
                _write_atomic(self.paths.active_config, old_config)
                self._save_metadata(previous_metadata)
            except Exception as rollback_error:
                raise AccountError(
                    f"Switch failed and automatic rollback also failed: {rollback_error}"
                ) from error
            raise AccountError(f"Switch failed; active files were restored: {error}") from error
        return account

    def delete(self, account_id: str) -> dict:
        data = self._load_metadata()
        if data["current_account_id"] == account_id:
            raise AccountError("Cannot delete the current account. Switch first.")
        account = next((item for item in data["accounts"] if item["id"] == account_id), None)
        if account is None:
            raise AccountError("Selected account no longer exists.")
        data["accounts"] = [item for item in data["accounts"] if item["id"] != account_id]
        self._save_metadata(data)
        profile_dir = self.paths.profile_dir(account_id)
        for path in (profile_dir / "auth.json", profile_dir / "config.toml"):
            path.unlink(missing_ok=True)
        profile_dir.rmdir()
        return account


def codex_processes() -> list[str]:
    result = subprocess.run(
        ["pgrep", "-af", "codex"], capture_output=True, text=True, check=False
    )
    return [line for line in result.stdout.splitlines() if line]


class Menu:
    def __init__(self, manager: AccountManager):
        self.manager = manager

    def _choose_account(self, prompt: str, allow_new: bool = False) -> dict | None:
        accounts = self.manager.accounts()
        if accounts:
            print(prompt)
            for index, account in enumerate(accounts, start=1):
                print(f"  {index}. {account['name']} [{account['auth_type']}]")
        if allow_new:
            print("  N. Create a new account")
        choice = input("> ").strip().lower()
        if allow_new and choice == "n":
            return None
        try:
            return accounts[int(choice) - 1]
        except (ValueError, IndexError):
            raise AccountError("Please choose a listed account.")

    def _save_current(self) -> dict:
        account = self._choose_account("Save current credentials to:", allow_new=True)
        detected_type = self.manager.detect_auth_type()
        if account is None:
            name = input("New account name: ")
            answer = input(f"Authentication type [1=chatgpt, 2=api_key] (default {detected_type}): ").strip()
            auth_type = {"1": "chatgpt", "2": "api_key", "" : detected_type}.get(answer)
            if auth_type is None:
                raise AccountError("Please choose 1 or 2.")
            saved = self.manager.save_current(name, auth_type)
        else:
            answer = input(
                f"Overwrite {account['name']!r} with current credentials? [y/N] "
            ).strip().lower()
            if answer != "y":
                raise AccountError("Save cancelled.")
            saved = self.manager.save_current(
                account["name"], detected_type, account["id"]
            )
        print(f"Saved current credentials as {saved['name']!r}.")
        return saved

    def show_status(self) -> None:
        current = self.manager.current_account()
        print("Current saved account:", current["name"] if current else "not recorded")
        accounts = self.manager.accounts()
        if not accounts:
            print("No saved accounts.")
        for account in accounts:
            marker = "*" if current and account["id"] == current["id"] else " "
            print(f"{marker} {account['name']} [{account['auth_type']}]")

    def switch(self) -> None:
        print("First save the current Codex credentials.")
        self._save_current()
        target = self._choose_account("Switch to:")
        if target is None:
            raise AccountError("No target account selected.")
        processes = codex_processes()
        if processes:
            print("Running Codex processes detected:")
            for process in processes:
                print(f"  {process}")
            if input("Switch anyway? [y/N] ").strip().lower() != "y":
                print("Switch cancelled.")
                return
        self.manager.switch_to(target["id"])
        print(f"Switched to {target['name']!r}.")

    def delete(self) -> None:
        account = self._choose_account("Delete which account?")
        current = self.manager.current_account()
        if current and account["id"] == current["id"]:
            raise AccountError("Cannot delete the current account. Switch first.")
        confirmation = input(f"Type {account['name']!r} to permanently delete it: ")
        if confirmation != account["name"]:
            print("Delete cancelled.")
            return
        self.manager.delete(account["id"])
        print(f"Deleted {account['name']!r}.")

    def run(self) -> None:
        while True:
            print("\nCodex account manager")
            print("1. List accounts and status")
            print("2. Save current credentials")
            print("3. Save current credentials and switch")
            print("4. Delete an account")
            print("5. Exit")
            choice = input("> ").strip()
            try:
                if choice == "1":
                    self.show_status()
                elif choice == "2":
                    self._save_current()
                elif choice == "3":
                    self.switch()
                elif choice == "4":
                    self.delete()
                elif choice == "5":
                    return
                else:
                    print("Please choose 1 through 5.")
            except (AccountError, OSError) as error:
                print(f"Error: {error}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage and switch Codex accounts.")
    parser.add_argument(
        "--codex-dir", type=Path, default=Path.home() / ".codex",
        help="Codex configuration directory (default: ~/.codex).",
    )
    args = parser.parse_args()
    Menu(AccountManager(Paths(args.codex_dir))).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
