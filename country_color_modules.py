from __future__ import annotations

import colorsys
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from world_history_modules import (
    brace_balance,
    build_exact_adjacency,
    parse_state_provinces,
    read_text,
    top_level_blocks,
    write_text,
)

RGB = Tuple[int, int, int]
Lab = Tuple[float, float, float]


@dataclass
class CountryColorPlan:
    mode: str
    palette: List[RGB] = field(default_factory=list)
    state_colors: Dict[str, RGB] = field(default_factory=dict)
    subject_colors: Dict[str, RGB] = field(default_factory=dict)
    adjacency: Dict[str, Set[str]] = field(default_factory=dict)
    minimum_neighbour_distance: float = 0.0
    average_neighbour_distance: float = 0.0
    duplicate_neighbour_pairs: int = 0
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Perceptual colour helpers
# ---------------------------------------------------------------------------

def _srgb_to_linear(value: float) -> float:
    value /= 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: RGB) -> Lab:
    r, g, b = (_srgb_to_linear(channel) for channel in rgb)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    # D65 reference white.
    x /= 0.95047
    y /= 1.00000
    z /= 1.08883

    def f(value: float) -> float:
        delta = 6.0 / 29.0
        if value > delta ** 3:
            return value ** (1.0 / 3.0)
        return value / (3.0 * delta ** 2) + 4.0 / 29.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def lab_distance(a: Lab, b: Lab) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _relative_luminance(rgb: RGB) -> float:
    r, g, b = (_srgb_to_linear(channel) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hls_rgb(hue: float, lightness: float, saturation: float) -> RGB:
    red, green, blue = colorsys.hls_to_rgb(hue % 1.0, lightness, saturation)
    return tuple(max(0, min(255, round(channel * 255))) for channel in (red, green, blue))  # type: ignore[return-value]


def _candidate_colors(mode: str) -> List[RGB]:
    if mode == "vivid":
        saturations = (0.82, 0.94)
        lightnesses = (0.34, 0.46, 0.58)
        hue_steps = 48
    elif mode == "soft":
        saturations = (0.48, 0.62, 0.74)
        lightnesses = (0.50, 0.61, 0.70)
        hue_steps = 48
    else:
        saturations = (0.62, 0.76, 0.90)
        lightnesses = (0.36, 0.48, 0.60)
        hue_steps = 48

    result: List[RGB] = []
    seen: Set[RGB] = set()
    for offset in (0.0, 0.5 / hue_steps):
        for index in range(hue_steps):
            hue = (index / hue_steps + offset) % 1.0
            for saturation in saturations:
                for lightness in lightnesses:
                    rgb = _hls_rgb(hue, lightness, saturation)
                    luminance = _relative_luminance(rgb)
                    if not 0.055 <= luminance <= 0.76:
                        continue
                    if max(rgb) - min(rgb) < 45:
                        continue
                    if rgb not in seen:
                        seen.add(rgb)
                        result.append(rgb)
    return result


def make_palette(mode: str, rng: random.Random, count: int = 64) -> List[RGB]:
    candidates = _candidate_colors(mode)
    if len(candidates) < count:
        raise ValueError("Não foi possível gerar uma paleta grande o bastante.")
    labs = {rgb: rgb_to_lab(rgb) for rgb in candidates}

    # Start near a balanced luminance but with strong chroma.
    first = max(
        candidates,
        key=lambda rgb: (
            max(rgb) - min(rgb),
            -abs(_relative_luminance(rgb) - 0.34),
            rng.random(),
        ),
    )
    selected = [first]
    remaining = set(candidates)
    remaining.remove(first)
    while len(selected) < count:
        selected_labs = [labs[rgb] for rgb in selected]
        best = max(
            remaining,
            key=lambda rgb: (
                min(lab_distance(labs[rgb], other) for other in selected_labs),
                -abs(_relative_luminance(rgb) - 0.34),
                rng.random() * 0.25,
            ),
        )
        selected.append(best)
        remaining.remove(best)
    return selected


def _subject_variant(rgb: RGB, mode: str) -> RGB:
    red, green, blue = (channel / 255.0 for channel in rgb)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    if mode == "soft":
        lightness = max(0.28, lightness - 0.12)
        saturation = min(0.86, saturation + 0.08)
    else:
        lightness = max(0.24, lightness - 0.15)
        saturation = min(0.98, saturation + 0.06)
    return _hls_rgb(hue, lightness, saturation)


# ---------------------------------------------------------------------------
# State-colour assignment
# ---------------------------------------------------------------------------

def _distance_two(adjacency: Dict[str, Set[str]], state: str) -> Set[str]:
    direct = adjacency.get(state, set())
    result: Set[str] = set()
    for neighbour in direct:
        result.update(adjacency.get(neighbour, set()))
    result.discard(state)
    result.difference_update(direct)
    return result


def _assign_global(states: Sequence[str], palette: Sequence[RGB], rng: random.Random) -> Dict[str, RGB]:
    ordered = list(states)
    rng.shuffle(ordered)
    palette_order = list(palette)
    rng.shuffle(palette_order)
    assignments: Dict[str, RGB] = {}
    for index, state in enumerate(ordered):
        assignments[state] = palette_order[index % len(palette_order)]
    return assignments


def _assign_neighbour_contrast(
    states: Sequence[str],
    adjacency: Dict[str, Set[str]],
    palette: Sequence[RGB],
    rng: random.Random,
) -> Dict[str, RGB]:
    labs = {rgb: rgb_to_lab(rgb) for rgb in palette}
    assignments: Dict[str, RGB] = {}
    usage: Counter[RGB] = Counter()
    unassigned = set(states)
    distance_two = {state: _distance_two(adjacency, state) for state in states}

    while unassigned:
        def node_priority(state: str) -> Tuple[int, int, float]:
            neighbour_colors = {assignments[n] for n in adjacency.get(state, set()) if n in assignments}
            assigned_neighbours = sum(n in assignments for n in adjacency.get(state, set()))
            return (len(neighbour_colors), assigned_neighbours + len(adjacency.get(state, set())), rng.random())

        state = max(unassigned, key=node_priority)
        direct_colors = [assignments[n] for n in adjacency.get(state, set()) if n in assignments]
        second_colors = [assignments[n] for n in distance_two[state] if n in assignments]

        def candidate_score(rgb: RGB) -> Tuple[float, float, float, float]:
            if rgb in direct_colors:
                return (-10_000.0, 0.0, 0.0, 0.0)
            direct_distance = min(
                (lab_distance(labs[rgb], labs[other]) for other in direct_colors),
                default=120.0,
            )
            second_distance = min(
                (lab_distance(labs[rgb], labs[other]) for other in second_colors),
                default=100.0,
            )
            # Direct borders dominate. Distance-two separation prevents a whole
            # local cluster from using only one colour family.
            # Cap the distance reward once colours are already clearly distinct.
            # This prevents the graph from collapsing to only a handful of extreme
            # red/blue/green choices and keeps the political map varied.
            return (
                min(direct_distance, 78.0) * 3.0
                + min(second_distance, 58.0) * 0.55
                - usage[rgb] * 7.0,
                direct_distance,
                -usage[rgb],
                rng.random(),
            )

        selected = max(palette, key=candidate_score)
        assignments[state] = selected
        usage[selected] += 1
        unassigned.remove(state)
    return assignments


def _colour_metrics(assignments: Dict[str, RGB], adjacency: Dict[str, Set[str]]) -> Tuple[float, float, int]:
    distances: List[float] = []
    duplicates = 0
    labs = {rgb: rgb_to_lab(rgb) for rgb in set(assignments.values())}
    seen: Set[Tuple[str, str]] = set()
    for state, neighbours in adjacency.items():
        if state not in assignments:
            continue
        for neighbour in neighbours:
            if neighbour not in assignments:
                continue
            edge = tuple(sorted((state, neighbour)))
            if edge in seen:
                continue
            seen.add(edge)
            a, b = assignments[state], assignments[neighbour]
            if a == b:
                duplicates += 1
            distances.append(lab_distance(labs[a], labs[b]))
    if not distances:
        return 0.0, 0.0, duplicates
    return min(distances), sum(distances) / len(distances), duplicates


def build_country_color_plan(
    base: Path,
    states: Dict[str, object],
    strategic_plan: object,
    mode: str,
    seed: int,
) -> Optional[CountryColorPlan]:
    if mode == "keep":
        return None
    if mode not in {"global_contrast", "neighbour_contrast", "vivid", "soft"}:
        raise ValueError(f"Modo de cores inválido: {mode}")

    rng = random.Random(f"bwg-v6.5-colours:{seed}:{mode}")
    palette_mode = mode if mode in {"vivid", "soft"} else "neighbour_contrast"
    palette = make_palette(palette_mode, rng, 64)
    state_names = sorted(states)

    adjacency: Dict[str, Set[str]] = {
        state: set(getattr(strategic_plan, "adjacency", {}).get(state, set()))
        for state in state_names
    }
    has_exact_edges = any(adjacency.values())
    warnings: List[str] = []

    if mode in {"neighbour_contrast", "vivid", "soft"} and not has_exact_edges:
        province_map = base / "map_data/provinces.png"
        if not province_map.exists():
            raise FileNotFoundError(
                "O modo de contraste entre países vizinhos precisa de map_data/provinces.png. "
                "Selecione a instalação completa do Victoria 3."
            )
        provinces, _macro = parse_state_provinces(base / "map_data/state_regions")
        adjacency, _centroids, _size = build_exact_adjacency(
            state_names,
            provinces,
            base / "common/strait_definitions",
            province_map,
        )
        has_exact_edges = True

    if mode == "global_contrast":
        assignments = _assign_global(state_names, palette, rng)
        if not has_exact_edges:
            warnings.append("Contraste global gerado sem validar fronteiras, pois provinces.png não estava disponível.")
    else:
        assignments = _assign_neighbour_contrast(state_names, adjacency, palette, rng)

    subject_colors = {state: _subject_variant(rgb, palette_mode) for state, rgb in assignments.items()}
    minimum, average, duplicates = _colour_metrics(assignments, adjacency)
    if duplicates:
        warnings.append(f"{duplicates} fronteiras de estado receberam cores idênticas; isso não deveria ocorrer.")
    return CountryColorPlan(
        mode=mode,
        palette=list(palette),
        state_colors=assignments,
        subject_colors=subject_colors,
        adjacency=adjacency,
        minimum_neighbour_distance=minimum,
        average_neighbour_distance=average,
        duplicate_neighbour_pairs=duplicates,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Mod-output writers
# ---------------------------------------------------------------------------

def _name_for_state(state: str) -> str:
    return "bwg_country_" + state.lower().removeprefix("state_")


def _subject_name_for_state(state: str) -> str:
    return "bwg_subject_" + state.lower().removeprefix("state_")


def _rgb_literal(rgb: RGB) -> str:
    return f"{{ {rgb[0]} {rgb[1]} {rgb[2]} }}"


def _named_rgb(rgb: RGB) -> str:
    return "rgb { " + " ".join(f"{channel / 255.0:.4f}" for channel in rgb) + " }"


def write_named_palette(plan: CountryColorPlan, mod_root: Path) -> None:
    lines = ["colors = {"]
    for state in sorted(plan.state_colors):
        lines.append(f"    {_name_for_state(state)} = {_named_rgb(plan.state_colors[state])}")
        lines.append(f"    {_subject_name_for_state(state)} = {_named_rgb(plan.subject_colors[state])}")
    lines.append("}")
    write_text(
        mod_root / "common/named_colors/99_bwg_generated_country_colors.txt",
        "\n".join(lines) + "\n",
        bom=True,
    )


def write_dynamic_state_colors(plan: CountryColorPlan, mod_root: Path) -> None:
    lines: List[str] = [
        "# Generated by Randomised World History Generator v6.8.",
        "# The capital-state graph is coloured so neighbouring starting countries",
        "# do not collapse into one pastel colour family.",
        "",
    ]
    for state in sorted(plan.state_colors):
        state_id = state.lower()
        lines.extend([
            f"bwg_subject_colour_{state_id} = {{",
            f'    color = "{_subject_name_for_state(state)}"',
            "    possible = {",
            "        is_subject = yes",
            f"        capital ?= {{ state_region = s:{state} }}",
            "    }",
            "}",
            "",
            f"bwg_country_colour_{state_id} = {{",
            f'    color = "{_name_for_state(state)}"',
            "    possible = {",
            "        is_subject = no",
            "        NOT = { has_variable = randomiser_formed_country }",
            f"        capital ?= {{ state_region = s:{state} }}",
            "    }",
            "}",
            "",
        ])
    target = mod_root / "common/dynamic_country_map_colors/00_adynamic_randomiser_country_colours.txt"
    write_text(target, "\n".join(lines), bom=True)


def patch_dynamic_country_fallback_colors(plan: CountryColorPlan, mod_root: Path) -> int:
    path = mod_root / "common/scripted_effects/02_random_stuff.txt"
    if not path.exists():
        return 0
    text = read_text(path)
    palette = list(plan.palette)
    rng = random.Random(f"bwg-v6.5-fallback:{len(text)}:{plan.mode}")
    rng.shuffle(palette)
    counter = 0

    pattern = re.compile(r"(?m)^(\s*)color\s*=\s*\{\s*\d+\s+\d+\s+\d+\s*\}")

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        rgb = palette[counter % len(palette)]
        counter += 1
        return f"{match.group(1)}color = {_rgb_literal(rgb)}"

    text = pattern.sub(replace, text)
    if brace_balance(text) != 0:
        raise ValueError("A substituição das cores de fallback deixou o scripted effect inválido.")
    write_text(path, text, bom=True)
    return counter


def patch_template_country_definitions(plan: CountryColorPlan, mod_root: Path) -> int:
    patched = 0
    name_pattern = r"(?:REPLACE_OR_CREATE:|INJECT_OR_CREATE:)?[A-Za-z0-9_]+"
    for path in sorted((mod_root / "common/country_definitions").glob("*.txt")):
        text = read_text(path)
        operations: List[Tuple[int, int, str]] = []
        for _name, block_start, _open, _close, block_end in top_level_blocks(text, name_pattern):
            block = text[block_start:block_end]
            capital = re.search(r"(?m)^\s*capital\s*=\s*(STATE_[A-Z0-9_]+)", block)
            color = re.search(r"(?m)^\s*color\s*=\s*\{\s*\d+\s+\d+\s+\d+\s*\}", block)
            if not capital or not color or capital.group(1) not in plan.state_colors:
                continue
            rgb = plan.state_colors[capital.group(1)]
            local_start, local_end = color.span()
            replacement = re.sub(r"\{[\s\d]+\}", _rgb_literal(rgb), color.group(0))
            operations.append((block_start + local_start, block_start + local_end, replacement))
            patched += 1
        for op_start, op_end, replacement in sorted(operations, reverse=True):
            text = text[:op_start] + replacement + text[op_end:]
        if operations:
            if brace_balance(text) != 0:
                raise ValueError(f"Cores deixaram a definição nacional inválida: {path.name}")
            write_text(path, text, bom=True)
    return patched


def write_color_report(plan: CountryColorPlan, mod_root: Path, fallback_count: int, definition_count: int) -> None:
    labels = {
        "global_contrast": "contraste global",
        "neighbour_contrast": "contraste entre vizinhos",
        "vivid": "paleta vívida de alto contraste",
        "soft": "paleta suave distinguível",
    }
    has_edges = any(plan.adjacency.values())
    lines = [
        "RANDOMISED WORLD — COUNTRY COLOR REPORT v6.8",
        f"Modo: {labels.get(plan.mode, plan.mode)}",
        f"Cores-base da paleta: {len(plan.palette)}",
        f"Estados com cor procedural: {len(plan.state_colors)}",
    ]
    if has_edges:
        lines.extend([
            f"Pares vizinhos com cor idêntica: {plan.duplicate_neighbour_pairs}",
            f"Distância perceptual mínima entre estados vizinhos: {plan.minimum_neighbour_distance:.2f}",
            f"Distância perceptual média entre estados vizinhos: {plan.average_neighbour_distance:.2f}",
        ])
    else:
        lines.append("Fronteiras analisadas: não; o modo global não exige provinces.png.")
    lines.extend([
        f"Cores de fallback substituídas em create_dynamic_country: {fallback_count}",
        f"Definições nacionais auxiliares recoloridas pela capital: {definition_count}",
        "",
        "Observações:",
        "- A cor principal de um país procedural segue seu estado-capital.",
        "- Países sujeitos usam uma variante mais escura da cor do próprio estado-capital.",
        "- Territórios coloniais pertencentes à metrópole mantêm a cor da própria metrópole.",
        "- Uma relação cromática automática entre vassalo e suserano não é aplicada, pois o motor não expõe a cor do suserano como valor reutilizável em script.",
    ])
    if plan.warnings:
        lines.extend(["", "Avisos:", *[f"- {warning}" for warning in plan.warnings]])
    write_text(mod_root / "COUNTRY_COLOR_REPORT_PT-BR.txt", "\n".join(lines) + "\n")


def apply_country_color_plan(plan: Optional[CountryColorPlan], mod_root: Path) -> Tuple[int, int]:
    if plan is None:
        return 0, 0
    write_named_palette(plan, mod_root)
    write_dynamic_state_colors(plan, mod_root)
    fallback_count = patch_dynamic_country_fallback_colors(plan, mod_root)
    definition_count = patch_template_country_definitions(plan, mod_root)
    write_color_report(plan, mod_root, fallback_count, definition_count)
    return fallback_count, definition_count


def validate_country_color_plan(plan: Optional[CountryColorPlan], mod_root: Path) -> List[str]:
    if plan is None:
        return ["Cores dos países: mantidas conforme o mod-base."]
    if not plan.state_colors:
        raise ValueError("O plano de cores não possui estados.")
    geographic_mode = plan.mode in {"neighbour_contrast", "vivid", "soft"}
    has_edges = any(plan.adjacency.values())
    if geographic_mode and plan.duplicate_neighbour_pairs:
        raise ValueError(
            f"O plano de cores possui {plan.duplicate_neighbour_pairs} fronteiras com cores idênticas."
        )
    named = mod_root / "common/named_colors/99_bwg_generated_country_colors.txt"
    dynamic = mod_root / "common/dynamic_country_map_colors/00_adynamic_randomiser_country_colours.txt"
    if not named.exists() or brace_balance(read_text(named)) != 0:
        raise ValueError("Paleta procedural ausente ou inválida.")
    if not dynamic.exists() or brace_balance(read_text(dynamic)) != 0:
        raise ValueError("Mapa dinâmico de cores ausente ou inválido.")
    label = {
        "global_contrast": "contraste global",
        "neighbour_contrast": "contraste entre vizinhos",
        "vivid": "vívidas / alto contraste",
        "soft": "suaves, porém distinguíveis",
    }.get(plan.mode, plan.mode)
    if geographic_mode and has_edges and plan.minimum_neighbour_distance < 18.0:
        raise ValueError(
            f"Contraste mínimo insuficiente entre vizinhos: {plan.minimum_neighbour_distance:.2f}."
        )
    if geographic_mode:
        return [
            f"Cores dos países: {label}; {len(plan.palette)} cores-base; nenhuma fronteira estadual com cor idêntica",
            f"Contraste perceptual entre vizinhos: mínimo {plan.minimum_neighbour_distance:.1f}, média {plan.average_neighbour_distance:.1f}",
        ]
    messages = [f"Cores dos países: {label}; {len(plan.palette)} cores-base distribuídas globalmente"]
    if has_edges:
        messages.append(
            f"Diagnóstico de fronteiras (não otimizado neste modo): {plan.duplicate_neighbour_pairs} pares idênticos; distância média {plan.average_neighbour_distance:.1f}"
        )
    else:
        messages.append("Fronteiras não foram analisadas no modo de contraste global.")
    return messages
