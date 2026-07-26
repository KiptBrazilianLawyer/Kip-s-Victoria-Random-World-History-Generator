from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from tkinter import Tk, filedialog, messagebox

from world_history_modules import (
    StrategicRegionPlan,
    parse_state_provinces,
    parse_strategic_region_definitions,
    write_strategic_region_localization,
)


def resolve_game_dir(path: Path) -> Path:
    path = path.resolve()
    if (path / "game" / "common" / "strategic_regions").is_dir():
        return path / "game"
    if (path / "common" / "strategic_regions").is_dir():
        return path
    raise ValueError(
        "A pasta selecionada não parece ser a instalação do Victoria 3 nem a pasta game."
    )


def build_plan(game_dir: Path, mod_dir: Path) -> StrategicRegionPlan:
    base_regions, _ = parse_strategic_region_definitions(game_dir / "common/strategic_regions")
    mod_region_dir = mod_dir / "common/strategic_regions"
    if not mod_region_dir.is_dir():
        raise ValueError("O mod selecionado não contém common/strategic_regions.")
    mod_regions, _ = parse_strategic_region_definitions(mod_region_dir)
    regions = dict(base_regions)
    regions.update(mod_regions)

    base_state_provinces, _ = parse_state_provinces(game_dir / "map_data/state_regions")
    mod_state_dir = mod_dir / "map_data/state_regions"
    if mod_state_dir.is_dir():
        mod_state_provinces, _ = parse_state_provinces(mod_state_dir)
        base_state_provinces.update(mod_state_provinces)

    province_to_state: dict[str, str] = {}
    for state, provinces in base_state_provinces.items():
        for province in provinces:
            province_to_state[province.lower()] = state

    assignments: dict[str, list[str]] = {}
    capitals: dict[str, str] = {}
    capital_states: dict[str, str] = {}
    for key, definition in regions.items():
        assignments[key] = list(definition.states)
        capitals[key] = definition.capital_province
        capital_state = province_to_state.get(definition.capital_province.lower())
        if not capital_state and definition.states:
            capital_state = definition.states[0]
        if capital_state:
            capital_states[key] = capital_state

    if not assignments:
        raise ValueError("Nenhuma região estratégica foi encontrada no mod.")

    return StrategicRegionPlan(
        mode="localization_repair",
        regions=regions,
        assignments=assignments,
        capitals=capitals,
        adjacency={},
        components={},
        component_sizes=Counter(),
        capital_states=capital_states,
        state_provinces=base_state_provinces,
        warnings=[],
    )


def repair(game_path: Path, mod_path: Path) -> int:
    game_dir = resolve_game_dir(game_path)
    mod_dir = mod_path.resolve()
    if not (mod_dir / "common").is_dir():
        raise ValueError("A pasta selecionada não parece ser a raiz de um mod gerado.")
    plan = build_plan(game_dir, mod_dir)
    write_strategic_region_localization(plan, game_dir, mod_dir)
    return len(plan.regions)


def gui() -> int:
    root = Tk()
    root.withdraw()
    root.update()
    game = filedialog.askdirectory(
        title="Selecione Victoria 3 ou Victoria 3/game"
    )
    if not game:
        return 1
    mod = filedialog.askdirectory(
        title="Selecione a pasta do mundo gerado dentro de Victoria 3/mod"
    )
    if not mod:
        return 1
    try:
        count = repair(Path(game), Path(mod))
    except Exception as exc:  # show a useful Windows dialog
        messagebox.showerror("Erro", str(exc))
        return 2
    messagebox.showinfo(
        "Localização corrigida",
        f"Foram geradas traduções substitutas para {count} regiões estratégicas.\n\n"
        "Feche completamente o Victoria 3 e abra uma nova campanha para atualizar o mapa.",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Corrige a localização multilíngue das regiões estratégicas de um mundo gerado."
    )
    parser.add_argument("--game", type=Path)
    parser.add_argument("--mod", type=Path)
    args = parser.parse_args()
    if args.game and args.mod:
        count = repair(args.game, args.mod)
        print(f"Localização corrigida para {count} regiões estratégicas.")
        return 0
    return gui()


if __name__ == "__main__":
    sys.exit(main())
