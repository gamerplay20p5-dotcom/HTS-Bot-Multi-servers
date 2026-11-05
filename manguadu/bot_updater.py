"""Atualizacao segura do checkout Python executada pela VM."""

from __future__ import annotations

from .i18n import t

import asyncio
import base64
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import clean_text


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "data" / "update-bot.log"


@dataclass
class ProcessResult:
    ok: bool
    code: int | None
    stdout: str
    stderr: str
    error: str = ""


class BotUpdater:
    """Git pull, instala dependencias Python e reinicia o processo PM2."""

    def __init__(self, root: str | Path = PROJECT_ROOT, environment: dict[str, str] | None = None):
        self.root = Path(root).resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self.log_file = Path(self.environment.get("BOT_UPDATE_LOG_FILE") or self.root / "data" / "update-bot.log")
        self.timeout = max(60, int(_number(self.environment.get("BOT_UPDATE_TIMEOUT_MS"), 600_000) / 1000))

    @property
    def app_name(self) -> str:
        return clean_text(self.environment.get("PM2_APP_NAME")) or "friendhost-pz-bot"

    @property
    def branch(self) -> str:
        return clean_text(self.environment.get("BOT_UPDATE_BRANCH"))

    @property
    def auto_update_enabled(self) -> bool:
        return clean_text(self.environment.get("BOT_AUTO_UPDATE_ENABLED")).lower() in {"1", "true", "yes", "on"}

    @property
    def auto_update_interval_ms(self) -> int:
        return max(60_000, int(_number(self.environment.get("BOT_AUTO_UPDATE_INTERVAL_MS"), 120_000)))

    @property
    def github_auth_configured(self) -> bool:
        return bool(clean_text(self.environment.get("BOT_GITHUB_TOKEN") or self.environment.get("GITHUB_TOKEN")))

    @property
    def python_runtime_configured(self) -> bool:
        return clean_text(self.environment.get("BOT_PM2_RUNTIME")).lower() == "python"

    def status(self) -> dict[str, Any]:
        return {
            "projectRoot": str(self.root),
            "scriptPath": str(self.root / "scripts" / "update_bot.py"),
            "scriptExists": (self.root / "scripts" / "update_bot.py").is_file(),
            "pm2AppName": self.app_name,
            "pm2Runtime": "python" if self.python_runtime_configured else t('nao configurado como Python'),
            "timeoutSeconds": self.timeout,
            "autoUpdateEnabled": self.auto_update_enabled,
            "autoUpdateIntervalMs": self.auto_update_interval_ms,
            "gitHubAuthConfigured": self.github_auth_configured,
            "logFile": str(self.log_file),
        }

    def read_last_log(self, limit: int = 1800) -> str:
        try:
            return _truncate(self.log_file.read_text(encoding="utf-8", errors="replace"), limit) or t('Ainda nao ha log de atualizacao.')
        except OSError:
            return t('Ainda nao ha log de atualizacao.')

    def _git_environment(self) -> dict[str, str]:
        env = dict(self.environment)
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = clean_text(env.get("BOT_GITHUB_TOKEN") or env.get("GITHUB_TOKEN"))
        if token:
            remote = _sync_git_remote(self.root, env)
            if remote.startswith("https://github.com/"):
                basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
                existing = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
                env["GIT_CONFIG_COUNT"] = str(existing + 1)
                env[f"GIT_CONFIG_KEY_{existing}"] = "http.https://github.com/.extraheader"
                env[f"GIT_CONFIG_VALUE_{existing}"] = f"AUTHORIZATION: basic {basic}"
        return env

    async def _run(self, *args: str, timeout: int | None = None, env: dict[str, str] | None = None) -> ProcessResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.root,
                env=env or self.environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout or self.timeout)
            return ProcessResult(process.returncode == 0, process.returncode, stdout.decode("utf-8", "replace").strip(), stderr.decode("utf-8", "replace").strip())
        except asyncio.TimeoutError:
            return ProcessResult(False, None, "", "", ''.join((format(t('Comando excedeu o limite de '), ''), format(timeout or self.timeout, ''), 's.')))
        except OSError as exc:
            return ProcessResult(False, None, "", "", str(exc))

    async def check_for_update(self) -> dict[str, Any]:
        if not (self.root / ".git").exists():
            return {"ok": False, "error": t('A pasta do bot nao e um repositorio Git.')}
        git_env = self._git_environment()
        fetched = await self._run("git", "fetch", "--prune", "origin", env=git_env)
        if not fetched.ok:
            return {"ok": False, "error": fetched.error or fetched.stderr or t('git fetch falhou.')}
        local = await self._run("git", "rev-parse", "HEAD")
        if not local.ok:
            return {"ok": False, "error": local.error or local.stderr or t('Nao foi possivel ler o commit local.')}
        branch_result = await self._run("git", "branch", "--show-current")
        branch = self.branch or branch_result.stdout
        if not branch:
            return {"ok": False, "error": t('Nao foi possivel identificar a branch.')}
        exists = await self._run("git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        if not exists.ok and branch != "main":
            fallback = await self._run("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main")
            if fallback.ok:
                branch = "main"
        remote = await self._run("git", "rev-parse", f"origin/{branch}")
        if not remote.ok:
            return {"ok": False, "error": remote.error or remote.stderr or ''.join((format(t('Branch remota origin/'), ''), format(branch, ''), format(t(' nao encontrada.'), '')))}
        return {"ok": True, "branch": branch, "local": local.stdout, "remote": remote.stdout, "hasUpdate": local.stdout != remote.stdout}

    async def update(self, *, skip_restart: bool = False) -> dict[str, Any]:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [t('Atualizacao Python iniciada.'), ''.join((format(t('Projeto: '), ''), format(self.root, ''))), f"PM2 app: {self.app_name}"]

        def finish(ok: bool, error: str = "") -> dict[str, Any]:
            if error:
                lines.append(f"ERRO: {error}")
            lines.append(t('Atualizacao concluida.') if ok else t('Atualizacao abortada.'))
            self._append_log("\n".join(lines))
            return {"ok": ok, "error": error, "output": _truncate("\n".join(lines), 1800)}

        if not skip_restart and not self.python_runtime_configured:
            return finish(False, t('PM2 ainda nao esta configurado para executar python -m manguadu --bot. Defina BOT_PM2_RUNTIME=python depois de configurar o processo.'))

        if shutil.which("git") is None:
            return finish(False, t('git nao encontrado na VM.'))
        if not (self.root / ".git").exists():
            return finish(False, t('A pasta do bot nao e um repositorio Git.'))
        status = await self._run("git", "status", "--porcelain", "--untracked-files=no")
        if not status.ok:
            return finish(False, status.error or status.stderr or t('Nao foi possivel verificar alteracoes locais.'))
        if status.stdout:
            lines.append(t('Alteracoes locais rastreadas detectadas; o pull foi cancelado para preservar os arquivos.'))
            return finish(False, t('Existem alteracoes locais rastreadas na VM.'))
        current = await self._run("git", "rev-parse", "--short", "HEAD")
        lines.append(''.join((format(t('Commit atual: '), ''), format(current.stdout or t('desconhecido'), ''))))
        branch = self.branch
        if not branch:
            result = await self._run("git", "branch", "--show-current")
            branch = result.stdout
        if not branch:
            return finish(False, t('Nao foi possivel identificar a branch atual.'))
        git_env = self._git_environment()
        fetched = await self._run("git", "fetch", "--prune", "origin", env=git_env)
        if not fetched.ok:
            return finish(False, fetched.error or fetched.stderr or t('git fetch falhou; verifique o remote e as permissoes.'))
        branch_check = await self._run("git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        if not branch_check.ok and branch != "main":
            fallback = await self._run("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main")
            if fallback.ok:
                lines.append(''.join((format(t('Branch remota '), ''), format(branch, ''), format(t(' nao encontrada; usando main.'), ''))))
                branch = "main"
        pull = await self._run("git", "pull", "--ff-only", "origin", branch, env=git_env)
        if not pull.ok:
            return finish(False, pull.error or pull.stderr or ''.join((format(t('Nao foi possivel atualizar a branch '), ''), format(branch, ''), '.')))
        lines.extend(filter(None, (pull.stdout, pull.stderr)))
        install = await self._run(sys.executable, "-m", "pip", "install", "-e", ".")
        if not install.ok:
            return finish(False, install.error or install.stderr or t('Falha ao instalar dependencias Python.'))
        lines.extend(filter(None, (install.stdout, install.stderr)))
        compile_result = await self._run(sys.executable, "-m", "compileall", "-q", "manguadu", "scripts/update_bot.py")
        if not compile_result.ok:
            return finish(False, compile_result.error or compile_result.stderr or t('Validacao de sintaxe Python falhou.'))
        lines.append(t('Modulos Python compilados com sucesso.'))
        if not skip_restart:
            pm2 = clean_text(self.environment.get("PM2_BIN")) or "pm2"
            restarted = await self._run(pm2, "restart", self.app_name, "--update-env")
            if not restarted.ok:
                return finish(False, restarted.error or restarted.stderr or t('Falha ao reiniciar o processo PM2.'))
            lines.extend(filter(None, (restarted.stdout, restarted.stderr)))
        else:
            lines.append(t('Reinicio omitido; o comando Discord cuidara do restart.'))
        return finish(True)

    def _append_log(self, message: str) -> None:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with self.log_file.open("a", encoding="utf-8") as log:
            log.write(f"\n==== {timestamp} ====\n{message}\n")


def _number(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _sync_git_remote(root: Path, env: dict[str, str]) -> str:
    import subprocess

    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, env=env, capture_output=True, text=True, timeout=10, check=False)
        return clean_text(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
