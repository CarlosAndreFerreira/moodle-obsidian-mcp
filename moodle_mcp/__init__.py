"""Servidor MCP para Moodle com sincronização para o Obsidian.

Submódulos:
    config: Configuração em camadas (ambiente > ficheiro > omissões).
    server: O servidor MCP e as suas ferramentas.
    sanitize: Limpeza do conteúdo de terceiros (prompt injection, Vault).
    login: Login pelo navegador com passport de uso único.
    token_catcher: Handler do esquema `moodlemobile://` para captura de token via SSO.
"""

__version__ = "0.7.2"
__all__ = ["__version__"]
