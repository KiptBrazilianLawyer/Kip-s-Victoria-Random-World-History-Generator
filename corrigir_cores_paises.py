from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from tkinter import Tk, filedialog, messagebox, simpledialog

from country_color_modules import (
    apply_country_color_plan,
    build_country_color_plan,
    validate_country_color_plan,
)
from generator_core import parse_state_regions


class EmptyPlan:
    def __init__(self, states: list[str]) -> None:
        self.adjacency = {state: set() for state in states}


def resolve_base(selected: Path) -> Path:
    if (selected / "game/map_data/state_regions").exists():
        return selected / "game"
    if (selected / "map_data/state_regions").exists():
        return selected
    raise FileNotFoundError("Não encontrei game/map_data/state_regions na pasta selecionada.")


def read_seed(mod_root: Path) -> int | None:
    for filename in ("BALANCED_WORLD_REPORT.txt", "HISTORIA_DO_MUNDO_PT-BR.txt"):
        path = mod_root / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        match = re.search(r"(?im)^\s*Seed\s*:\s*(\d+)", text)
        if match:
            return int(match.group(1))
    match = re.search(r"(\d{3,})", mod_root.name)
    return int(match.group(1)) if match else None


def make_backup(mod_root: Path) -> Path:
    backup = mod_root / "BACKUP_CORES_ANTES_V6_4.zip"
    targets = [
        mod_root / "common/dynamic_country_map_colors/00_adynamic_randomiser_country_colours.txt",
        mod_root / "common/named_colors/99_bwg_generated_country_colors.txt",
        mod_root / "common/scripted_effects/02_random_stuff.txt",
    ]
    targets.extend(sorted((mod_root / "common/country_definitions").glob("*.txt")))
    with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in targets:
            if path.exists():
                archive.write(path, path.relative_to(mod_root).as_posix())
    return backup


def main() -> int:
    root = Tk()
    root.withdraw()
    messagebox.showinfo(
        "Correção de cores v6.8",
        "Selecione primeiro a pasta completa do Victoria 3 e depois a pasta do mundo gerado.",
    )
    game_selected = filedialog.askdirectory(title="Pasta completa do Victoria 3")
    if not game_selected:
        return 0
    mod_selected = filedialog.askdirectory(title="Pasta do mundo gerado dentro da pasta mod")
    if not mod_selected:
        return 0

    choice = simpledialog.askstring(
        "Modo de cores",
        "Digite o número do modo:\n\n"
        "1 — Contraste global\n"
        "2 — Contraste entre vizinhos (recomendado)\n"
        "3 — Vívidas / alto contraste\n"
        "4 — Suaves, mas distinguíveis",
        initialvalue="2",
    )
    modes = {
        "1": "global_contrast",
        "2": "neighbour_contrast",
        "3": "vivid",
        "4": "soft",
    }
    if choice not in modes:
        messagebox.showerror("Erro", "Modo inválido.")
        return 1

    base = resolve_base(Path(game_selected))
    mod_root = Path(mod_selected)
    if not (mod_root / "common/scripted_effects/02_random_stuff.txt").exists():
        raise FileNotFoundError("A pasta selecionada não parece ser um mundo gerado pelo Randomised World.")
    seed = read_seed(mod_root)
    if seed is None:
        seed_text = simpledialog.askstring("Seed", "Digite a seed usada para gerar o mundo:")
        if not seed_text:
            return 0
        seed = int(seed_text)

    states, _texts, _blocks = parse_state_regions(base / "map_data/state_regions")
    plan = build_country_color_plan(base, states, EmptyPlan(sorted(states)), modes[choice], seed)
    backup = make_backup(mod_root)
    fallback_count, definition_count = apply_country_color_plan(plan, mod_root)
    validation = validate_country_color_plan(plan, mod_root)
    messagebox.showinfo(
        "Concluído",
        "As cores foram corrigidas sem alterar países, recursos ou fronteiras.\n\n"
        f"Backup: {backup}\n"
        f"Fallbacks recoloridos: {fallback_count}\n"
        f"Definições nacionais recoloridas: {definition_count}\n\n"
        + "\n".join(validation),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            messagebox.showerror("Erro", str(exc))
        except Exception:
            print(f"ERRO: {exc}", file=sys.stderr)
        raise
