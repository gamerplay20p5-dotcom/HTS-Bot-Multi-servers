"""Stable diagnostic codes; never include raw record values or absolute paths."""
from ..i18n import t

REASONS = {
    "invalid_key_value": "Linha inválida ou chave repetida no arquivo de personagem.",
    "unsupported_version": "Versão do registro ausente ou não suportada.",
    "character_mismatch": "O characterID do arquivo não corresponde ao índice.",
    "username_mismatch": "A conta no arquivo não corresponde à conta indicada no índice.",
    "invalid_boolean": "Campo booleano ausente ou inválido.",
    "invalid_number": "Métrica numérica inválida, negativa ou não finita.",
    "invalid_index": "A linha do índice não contém as quatro colunas esperadas.",
    "invalid_identifier": "Identificador de personagem ou conta inválido no índice.",
    "missing_file": "O índice aponta para um arquivo inexistente.",
    "permission_denied": "Permissão de leitura ou travessia negada para este arquivo.",
    "incomplete_write": "Arquivo sem quebra de linha final; possível gravação incompleta.",
    "changed_during_read": "O arquivo mudou durante a leitura. Será tentado novamente.",
    "file_size_limit": "O arquivo excede o limite de leitura permitido.",
    "invalid_utf8": "O arquivo não contém texto UTF-8 válido.",
    "unsafe_path": "Caminho externo, link simbólico ou arquivo não regular rejeitado.",
    "io_error": "Falha de leitura do arquivo.",
}


class InvalidRecord(ValueError):
    def __init__(self, code, *, field="", line=0):
        self.code, self.field, self.line = code, field, line
        super().__init__(code)


def error_code(error):
    if isinstance(error, InvalidRecord):
        return error.code
    if isinstance(error, FileNotFoundError):
        return "missing_file"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, UnicodeError):
        return "invalid_utf8"
    if isinstance(error, ValueError):
        return "unsafe_path"
    return "io_error"


def explanation(record):
    return t(REASONS.get(record["code"], REASONS["io_error"]))
