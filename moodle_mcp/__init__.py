"""Servidor MCP para Moodle com sincronização para o Obsidian.

Submódulos:
    config: Configuração em camadas (ambiente > ficheiro > omissões).
    server: O servidor MCP e as suas ferramentas.
    token_catcher: Handler do esquema `moodlemobile://` para captura de token via SSO.
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
