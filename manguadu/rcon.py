"""Cliente async do protocolo Source RCON usado pelo Project Zomboid."""

from __future__ import annotations

from .i18n import t

import asyncio
import secrets
import string
import struct
from dataclasses import dataclass
from .tenant_scope import current_scope


MAX_PACKET_SIZE = 4 * 1024 * 1024
MULTIPART_DRAIN_TIMEOUT = 0.35
SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_AUTH_RESPONSE = 2
MULTIPART_SENTINEL = b"\x00\x01\x00\x00"


@dataclass(frozen=True)
class RconResult:
    ok: bool
    output: str = ""
    error: str = ""
    stage: str = ""


def _packet(request_id: int, packet_type: int, body: str) -> bytes:
    content = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\0\0"
    return struct.pack("<i", len(content)) + content


async def _read_packet(reader: asyncio.StreamReader, timeout: float) -> tuple[int, int, bytes]:
    length = struct.unpack("<i", await asyncio.wait_for(reader.readexactly(4), timeout))[0]
    if length < 10 or length > MAX_PACKET_SIZE:
        raise ValueError(''.join((format(t('pacote com tamanho invalido ('), ''), format(length, ''), ' bytes)')))
    payload = await asyncio.wait_for(reader.readexactly(length), timeout)
    request_id, packet_type = struct.unpack("<ii", payload[:8])
    if not payload.endswith(b"\0\0"):
        raise ValueError(t('pacote sem terminador'))
    return request_id, packet_type, payload[8:-2]


async def _read_command_output(
    reader: asyncio.StreamReader,
    command_id: int,
    timeout: float,
) -> str:
    """Leia todas as partes da resposta, aceitando tambem servidores sem sentinel."""
    chunks: list[bytes] = []
    total = 0
    first_packet = True
    while True:
        try:
            packet_id, packet_type, body = await _read_packet(
                reader,
                timeout if first_packet else min(timeout, MULTIPART_DRAIN_TIMEOUT),
            )
        except TimeoutError:
            if chunks or not first_packet:
                break
            raise
        except asyncio.IncompleteReadError:
            # Alguns servidores fecham o socket apos uma resposta unica, sem
            # mandar o pacote sentinel usado para respostas multipart.
            if chunks or not first_packet:
                break
            raise
        first_packet = False
        if packet_type != SERVERDATA_RESPONSE_VALUE:
            raise ValueError(''.join((format(t('tipo de resposta inesperado ('), ''), format(packet_type, ''), ')')))
        if packet_id != command_id:
            raise ValueError(''.join((format(t('id de resposta inesperado ('), ''), format(packet_id, ''), ')')))
        if body == MULTIPART_SENTINEL or not body:
            break
        total += len(body)
        if total > MAX_PACKET_SIZE:
            raise ValueError(t('resposta excedeu o limite de 4 MiB'))
        chunks.append(body)
    return b"".join(chunks).decode("utf-8", errors="replace").strip()


async def send_rcon_command(host: str, port: int, password: str, command: str, timeout: float = 10) -> RconResult:
    scope = current_scope()
    bot = getattr(getattr(scope, "runtime", None), "bot", None)
    if bot is not None:
        async with bot._rcon_limiter:
            return await _send_rcon_command(host, port, password, command, timeout)
    return await _send_rcon_command(host, port, password, command, timeout)


async def _send_rcon_command(host: str, port: int, password: str, command: str, timeout: float = 10) -> RconResult:
    host = str(host or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if not host or not 1 <= port <= 65535 or not password:
        return RconResult(
            False,
            error=t('Configuracao incompleta: informe host, porta TCP (1 a 65535) e senha RCON.'),
            stage="config",
        )

    endpoint = f"{host}:{port}"
    writer: asyncio.StreamWriter | None = None
    stage = t('conexao TCP')
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)

        stage = t('autenticacao')
        auth_id = secrets.randbelow(2**30) + 1
        writer.write(_packet(auth_id, SERVERDATA_AUTH, password))
        await asyncio.wait_for(writer.drain(), timeout)

        # Alguns servidores Source enviam um RESPONSE_VALUE vazio antes da
        # confirmacao de autenticacao.
        for _ in range(4):
            packet_id, packet_type, _ = await _read_packet(reader, timeout)
            if packet_type == SERVERDATA_AUTH_RESPONSE:
                if packet_id == -1:
                    return RconResult(
                        False,
                        error=''.join((format(t('Senha RCON recusada por '), ''), format(endpoint, ''), format(t('. Confira RCON_PASSWORD e RCONPassword no servidor PZ.'), ''))),
                        stage=stage,
                    )
                if packet_id != auth_id:
                    return RconResult(
                        False,
                        error=''.join((format(t('Resposta de autenticacao invalida recebida de '), ''), format(endpoint, ''), '.')),
                        stage=stage,
                    )
                break
            if packet_type != SERVERDATA_RESPONSE_VALUE:
                return RconResult(
                    False,
                    error=''.join((format(t('Resposta inesperada durante autenticacao em '), ''), format(endpoint, ''), ' (tipo ', format(packet_type, ''), ').')),
                    stage=stage,
                )
        else:
            return RconResult(
                False,
                error=''.join((format(endpoint, ''), format(t(' nao confirmou a autenticacao RCON.'), ''))),
                stage=stage,
            )

        stage = "comando"
        command_id = (auth_id + 1) % (2**30) or 1
        # O segundo pacote vazio e parte do protocolo Source: permite ao
        # servidor delimitar respostas divididas em varios pacotes.
        writer.write(
            _packet(command_id, SERVERDATA_EXECCOMMAND, command)
            + _packet(command_id, SERVERDATA_RESPONSE_VALUE, "")
        )
        await asyncio.wait_for(writer.drain(), timeout)
        output = await _read_command_output(reader, command_id, timeout)
        return RconResult(True, output=output, stage=stage)
    except TimeoutError:
        return RconResult(
            False,
            error=''.join((format(t('Tempo esgotado durante '), ''), format(stage, ''), ' em ', format(endpoint, ''), format(t('. Verifique se a porta RCON TCP esta acessivel e se o servidor respondeu.'), ''))),
            stage=stage,
        )
    except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
        detail = str(exc) or exc.__class__.__name__
        return RconResult(
            False,
            error=''.join((format(t('Falha na etapa de '), ''), format(stage, ''), ' em ', format(endpoint, ''), ': ', format(detail, ''))),
            stage=stage,
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def generate_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def adduser_command(username: str, password: str) -> str:
    # O comando do PZ recebe argumentos entre aspas. Restricao evita injecao
    # de console e permite nicks usuais de Steam/PZ.
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError(t('Usuario PZ deve ter 3 a 32 caracteres: letras, numeros, _, . ou -.'))
    if not re.fullmatch(r"[A-Za-z0-9!@#$%^&*._-]{8,64}", password):
        raise ValueError(t('Senha deve ter 8 a 64 caracteres sem espaços, aspas ou barras.'))
    return f'adduser "{username}" "{password}"'
