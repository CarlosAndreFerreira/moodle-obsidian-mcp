"""Registo do esquema `moodlemobile://` no sistema operativo.

Em universidades com SSO federado, a página `/user/managetoken.php` está muitas
vezes escondida aos estudantes e não há onde copiar um token. O caminho que
resta é o fluxo da aplicação móvel: o navegador termina num redirecionamento
para `moodlemobile://token=<base64>`, e quem estiver registado no sistema
operativo como handler desse esquema recebe o URL.

Registar o handler é a única parte que depende do sistema operativo, e os três
mecanismos não se parecem:

* Linux (XDG): um ficheiro `.desktop` com `MimeType=x-scheme-handler/moodlemobile`
  em `~/.local/share/applications`, mais `xdg-mime default`.
* Windows: chaves em `HKEY_CURRENT_USER/Software/Classes/moodlemobile`. Fica no
  ramo do utilizador, pelo que não precisa de privilégios de administrador.
* macOS: um pacote `.app` mínimo com `CFBundleURLTypes` no `Info.plist`, dado a
  conhecer ao Launch Services com `lsregister`.

O conteúdo de cada artefacto é produzido por funções puras
(`desktop_entry`, `info_plist`, `windows_registry_values`), separadas da escrita,
para que o resultado dos três sistemas possa ser verificado em qualquer um deles.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from moodle_mcp import __version__

#: Nome do esquema de URL que o Moodle usa no fluxo da aplicação móvel.
SCHEME = "moodlemobile"

#: Nome do ficheiro `.desktop` em Linux.
DESKTOP_FILE = "moodle-token-catcher.desktop"

#: Nome do pacote de aplicação em macOS.
APP_BUNDLE = "Moodle Token Catcher.app"

#: Identificador do pacote em macOS e chave de registo em Windows.
BUNDLE_ID = "pt.moodle-mcp.token-catcher"

FRIENDLY_NAME = "Moodle Token Catcher"
DESCRIPTION = "Captura o token moodlemobile:// do navegador e sincroniza o Vault do Obsidian"


class HandlerError(RuntimeError):
    """O handler não pôde ser registado ou removido."""


# ==============================================================================
# LOCALIZAR O EXECUTÁVEL
# ==============================================================================


def catcher_command() -> list[str]:
    """Devolve o comando que processa um URL `moodlemobile://`.

    Procura o console script `moodle-mcp-token-catcher` primeiro ao lado do
    interpretador em uso, que é onde o pip e o uv o instalam — `bin/` num venv
    POSIX, `Scripts/` em Windows. O PATH só é consultado depois, porque invocar
    `<venv>/bin/moodle-mcp` não põe esse diretório no PATH e uma busca por PATH
    falharia precisamente no caso normal. Sem console script — numa árvore de
    código sem instalação — recorre a `<python> -m moodle_mcp.token_catcher`,
    que é equivalente.

    Returns:
        A lista de argumentos, sem o URL, que é acrescentado pelo sistema.

    """
    name = "moodle-mcp-token-catcher" + (".exe" if sys.platform == "win32" else "")
    beside = Path(sys.executable).parent / name
    if beside.exists():
        return [str(beside)]
    script = shutil.which(name)
    if script:
        return [script]
    return [sys.executable, "-m", "moodle_mcp.token_catcher"]


def _quote(part: str) -> str:
    """Envolve em aspas um componente de comando que possa ter espaços."""
    return f'"{part}"' if " " in part else part


# ==============================================================================
# ARTEFACTOS (FUNÇÕES PURAS, VERIFICÁVEIS EM QUALQUER SISTEMA)
# ==============================================================================


def desktop_entry(command: list[str]) -> str:
    """Produz o conteúdo do ficheiro `.desktop` para Linux.

    Args:
        command: O comando de `catcher_command()`.

    Returns:
        O texto do ficheiro, com `%u` para o URL e sem ícone na lista de
        aplicações (`NoDisplay`), por ser um handler e não um programa.

    """
    exec_line = " ".join(_quote(part) for part in command)
    return (
        "[Desktop Entry]\n"
        f"Name={FRIENDLY_NAME}\n"
        f"Comment={DESCRIPTION}\n"
        f"Exec={exec_line} %u\n"
        "Terminal=false\n"
        "Type=Application\n"
        f"MimeType=x-scheme-handler/{SCHEME};\n"
        "NoDisplay=true\n"
    )


def windows_registry_values(command: list[str]) -> list[tuple[str, str, str]]:
    """Produz as chaves de registo para Windows.

    O `URL Protocol` vazio é o que marca a chave como um esquema de URL, e o
    `%1` no comando é substituído pelo URL. Tudo em `HKEY_CURRENT_USER`, pelo
    que não é preciso ser administrador.

    Args:
        command: O comando de `catcher_command()`.

    Returns:
        Triplos `(subchave, nome_do_valor, valor)`, com `""` como nome para o
        valor predefinido da chave.

    """
    exec_line = " ".join(_quote(part) for part in command)
    base = f"Software\\Classes\\{SCHEME}"
    return [
        (base, "", f"URL:{FRIENDLY_NAME}"),
        (base, "URL Protocol", ""),
        (base, "FriendlyTypeName", FRIENDLY_NAME),
        (f"{base}\\shell\\open\\command", "", f'{exec_line} "%1"'),
    ]


def info_plist() -> str:
    """Produz o `Info.plist` do pacote `.app` para macOS.

    `LSUIElement` evita que o handler apareça na Dock ou roube o foco quando o
    navegador o invoca.

    Returns:
        O XML do `Info.plist`.

    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"\t<key>CFBundleName</key>\n\t<string>{FRIENDLY_NAME}</string>\n"
        f"\t<key>CFBundleIdentifier</key>\n\t<string>{BUNDLE_ID}</string>\n"
        "\t<key>CFBundleExecutable</key>\n\t<string>moodle-token-catcher</string>\n"
        "\t<key>CFBundleInfoDictionaryVersion</key>\n\t<string>6.0</string>\n"
        "\t<key>CFBundlePackageType</key>\n\t<string>APPL</string>\n"
        f"\t<key>CFBundleShortVersionString</key>\n\t<string>{__version__}</string>\n"
        "\t<key>LSUIElement</key>\n\t<true/>\n"
        "\t<key>CFBundleURLTypes</key>\n"
        "\t<array>\n\t\t<dict>\n"
        f"\t\t\t<key>CFBundleURLName</key>\n\t\t\t<string>{FRIENDLY_NAME}</string>\n"
        "\t\t\t<key>CFBundleURLSchemes</key>\n"
        f"\t\t\t<array>\n\t\t\t\t<string>{SCHEME}</string>\n\t\t\t</array>\n"
        "\t\t</dict>\n\t</array>\n"
        "</dict>\n</plist>\n"
    )


def app_launcher(command: list[str]) -> str:
    """Produz o executável de arranque do pacote `.app` para macOS.

    O `.app` precisa de um binário próprio; aqui é um script que reencaminha o
    URL para o comando real.

    Args:
        command: O comando de `catcher_command()`.

    Returns:
        O texto do script.

    """
    exec_line = " ".join(_quote(part) for part in command)
    return f'#!/bin/sh\nexec {exec_line} "$1"\n'


# ==============================================================================
# CAMINHOS POR SISTEMA
# ==============================================================================


def desktop_path() -> Path:
    """Caminho do `.desktop` em Linux, respeitando `XDG_DATA_HOME`."""
    data_home = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home).expanduser() / "applications" / DESKTOP_FILE


def app_bundle_path() -> Path:
    """Caminho do pacote `.app` em macOS, dentro de `~/Applications`."""
    return Path.home() / "Applications" / APP_BUNDLE


# ==============================================================================
# INSTALAÇÃO
# ==============================================================================


def _install_linux(command: list[str]) -> dict[str, object]:
    target = desktop_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desktop_entry(command), encoding="utf-8")

    steps: list[str] = [f"escrito {target}"]
    for argv in (
        ["update-desktop-database", str(target.parent)],
        ["xdg-mime", "default", DESKTOP_FILE, f"x-scheme-handler/{SCHEME}"],
    ):
        if not shutil.which(argv[0]):
            steps.append(f"{argv[0]} não encontrado; registo pode precisar de ser manual")
            continue
        result = subprocess.run(argv, capture_output=True, text=True)
        steps.append(
            f"{argv[0]} ok" if result.returncode == 0 else f"{argv[0]} falhou: {result.stderr.strip()}"
        )
    return {"path": str(target), "steps": steps}


def _install_windows(command: list[str]) -> dict[str, object]:
    import winreg  # Só existe em Windows.

    steps: list[str] = []
    for subkey, name, value in windows_registry_values(command):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        steps.append(f"HKCU\\{subkey}\\{name or '(predefinido)'} definido")
    return {"path": f"HKEY_CURRENT_USER\\Software\\Classes\\{SCHEME}", "steps": steps}


def _install_macos(command: list[str]) -> dict[str, object]:
    bundle = app_bundle_path()
    macos_dir = bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    (bundle / "Contents" / "Info.plist").write_text(info_plist(), encoding="utf-8")
    launcher = macos_dir / "moodle-token-catcher"
    launcher.write_text(app_launcher(command), encoding="utf-8")
    launcher.chmod(0o755)

    steps = [f"escrito {bundle}"]
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if Path(lsregister).exists():
        result = subprocess.run([lsregister, "-f", str(bundle)], capture_output=True, text=True)
        steps.append(
            "lsregister ok" if result.returncode == 0 else f"lsregister falhou: {result.stderr.strip()}"
        )
    else:
        steps.append("lsregister não encontrado; abre o .app uma vez para o registar")
    return {"path": str(bundle), "steps": steps}


def install() -> dict[str, object]:
    """Regista o handler `moodlemobile://` no sistema operativo em uso.

    Returns:
        Dicionário com `platform`, `command`, `path` e `steps` — o que foi feito,
        em linguagem que se possa mostrar ao utilizador.

    Raises:
        HandlerError: Se o sistema operativo não for suportado.

    """
    command = catcher_command()
    if sys.platform.startswith("linux"):
        result = _install_linux(command)
    elif sys.platform == "win32":
        result = _install_windows(command)
    elif sys.platform == "darwin":
        result = _install_macos(command)
    else:
        raise HandlerError(
            f"Sistema operativo '{sys.platform}' não suportado. "
            "Registra manualmente um handler para o esquema "
            f"'{SCHEME}://' que invoque: {' '.join(command)} <url>"
        )
    result["platform"] = platform.platform()
    result["command"] = " ".join(command)
    return result


# ==============================================================================
# REMOÇÃO
# ==============================================================================


def uninstall() -> dict[str, object]:
    """Remove o registo do handler, se existir.

    Returns:
        Dicionário com `removed` (o que foi removido) e `missing` (o que já não
        estava lá), para que a remoção seja idempotente e explícita.

    Raises:
        HandlerError: Se o sistema operativo não for suportado.

    """
    removed: list[str] = []
    missing: list[str] = []

    if sys.platform.startswith("linux"):
        target = desktop_path()
        if target.exists():
            target.unlink()
            removed.append(str(target))
        else:
            missing.append(str(target))
    elif sys.platform == "win32":
        import winreg

        for subkey in (
            f"Software\\Classes\\{SCHEME}\\shell\\open\\command",
            f"Software\\Classes\\{SCHEME}\\shell\\open",
            f"Software\\Classes\\{SCHEME}\\shell",
            f"Software\\Classes\\{SCHEME}",
        ):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
                removed.append(f"HKCU\\{subkey}")
            except FileNotFoundError:
                missing.append(f"HKCU\\{subkey}")
    elif sys.platform == "darwin":
        bundle = app_bundle_path()
        if bundle.exists():
            shutil.rmtree(bundle)
            removed.append(str(bundle))
        else:
            missing.append(str(bundle))
    else:
        raise HandlerError(f"Sistema operativo '{sys.platform}' não suportado.")

    return {"removed": removed, "missing": missing}
