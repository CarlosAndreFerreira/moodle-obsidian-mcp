"""Gera o ícone do plugin (plugin/.claude-plugin/icon.png).

Uso: `uv run --with pillow python tools/make_icon.py <saída.png>`. Um capelo sobre
uma folha de notas com uma tarefa feita; desenho original, sem logótipos de terceiros.
"""

import sys

from PIL import Image, ImageDraw

N = 1024
SS = 4  # supersampling para bordas suaves
W = N * SS


def s(v):
    """Coordenada de 1024 px na tela ampliada (supersampling)."""
    return int(v * SS)


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# Fundo: quadrado arredondado com gradiente diagonal índigo -> verde-azulado.
grad = Image.new("RGBA", (W, W))
top, bottom = (46, 38, 120), (22, 130, 128)
gd = ImageDraw.Draw(grad)
for y in range(W):
    t = y / (W - 1)
    gd.line(
        [(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)) + (255,)
    )
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=s(224), fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
white = (255, 255, 255, 255)
soft = (255, 255, 255, 235)
ink = (46, 38, 120, 255)

# Folha de notas.
d.rounded_rectangle([s(262), s(440), s(762), s(880)], radius=s(48), fill=soft)
# Linhas de texto.
for i, y in enumerate((590, 680, 770)):
    x0 = 400 if i == 0 else 330
    d.rounded_rectangle([s(x0), s(y - 14), s(690), s(y + 14)], radius=s(14), fill=(46, 38, 120, 70))
# Caixa com visto (tarefa feita) na primeira linha.
d.rounded_rectangle([s(318), s(558), s(380), s(620)], radius=s(12), fill=(22, 130, 128, 255))
d.line([(s(332), s(590)), (s(347), s(605)), (s(368), s(572))], fill=white, width=s(11), joint="curve")

# Capelo: base, tampo e borla, com contorno para se destacar da folha.
OUT = s(12)
base = [(s(340), s(330)), (s(684), s(330)), (s(684), s(436)), (s(512), s(492)), (s(340), s(436))]
d.polygon(base, fill=(232, 230, 247, 255), outline=ink, width=OUT)
top_pts = [(s(512), s(132)), (s(850), s(276)), (s(512), s(420)), (s(174), s(276))]
d.polygon(top_pts, fill=white, outline=ink, width=OUT)
gold = (255, 196, 64, 255)
d.line([(s(512), s(276)), (s(796), s(318)), (s(796), s(432))], fill=gold, width=s(16), joint="curve")
d.ellipse([s(772), s(420), s(820), s(468)], fill=gold)

img = img.resize((N, N), Image.LANCZOS)
img.save(sys.argv[1], optimize=True)
