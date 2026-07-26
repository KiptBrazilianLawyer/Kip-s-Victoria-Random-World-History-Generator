from __future__ import annotations

import os
import random
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from generator_core import GenerationCancelled, generate_world

BASE = Path(__file__).resolve().parent


def autodetect_game() -> str:
    candidates: list[Path] = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        root = os.environ.get(env)
        if root:
            candidates.extend(
                [
                    Path(root) / "Steam/steamapps/common/Victoria 3",
                    Path(root) / "Steam/steamapps/common/Victoria 3/game",
                ]
            )
    for path in candidates:
        if (path / "game/map_data/state_regions").exists() or (path / "map_data/state_regions").exists():
            return str(path)
    return ""


def default_output() -> str:
    return str(Path.home() / "Documents/Paradox Interactive/Victoria 3/mod")


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.content.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind_all("<MouseWheel>", self._mousewheel)

    def _sync_region(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)

    def _mousewheel(self, event: tk.Event) -> None:
        if self.winfo_exists():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Randomised World — World History Generator v6.15")
        self.geometry("980x900")
        self.minsize(800, 720)

        self.game = tk.StringVar(value=autodetect_game())
        self.output = tk.StringVar(value=default_output())
        self.seed = tk.StringVar(value=str(random.randrange(1, 2_147_483_647)))
        self.resources = tk.StringVar(value="plausible_gradual")
        self.resource_visibility = tk.StringVar(value="sparse")
        self.arable_land = tk.StringVar(value="global")
        self.arable_resources = tk.StringVar(value="natural")
        self.population = tk.StringVar(value="global")
        self.cultures = tk.StringVar(value="natural_blocks")
        self.buildings = tk.StringVar(value="balanced")
        self.technology = tk.StringVar(value="natural_spread")
        self.development = tk.StringVar(value="normal")
        self.fiscal_safety = tk.StringVar(value="strict")
        self.military_economy = tk.StringVar(value="economic_conservative")
        self.intensity = tk.StringVar(value="medium")
        self.strategic_regions = tk.StringVar(value="contiguous")
        self.country_colors = tk.StringVar(value="neighbour_contrast")
        self.country_scale = tk.StringVar(value="balanced")
        self.power_distribution = tk.StringVar(value="balanced_continents")
        self.companies = tk.StringVar(value="natural_dynamic")
        self.strategic_needs = tk.StringVar(value="natural")
        self.diplomacy = tk.StringVar(value="natural_relations")
        self.civilization_centers = tk.StringVar(value="sparse")
        self.overseas_territories = tk.StringVar(value="rare_colonial")
        self.subjects = tk.StringVar(value="very_rare")
        self.historical_remnants = tk.StringVar(value="dissolve")
        self.foreign_investment = tk.StringVar(value="block_ai")
        self._build_ui()

    def _path_row(self, parent: ttk.LabelFrame, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(
            parent,
            text="Procurar...",
            command=lambda: variable.set(filedialog.askdirectory() or variable.get()),
        ).grid(row=row, column=2, padx=8, pady=6)

    def _combo_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        choices: list[tuple[str, str]],
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", padx=10, pady=7)
        labels = [label_text for label_text, _ in choices]
        value_by_label = dict(choices)
        current = next((label_text for label_text, value in choices if value == variable.get()), labels[0])
        shown = tk.StringVar(value=current)
        combo = ttk.Combobox(parent, textvariable=shown, values=labels, state="readonly")
        combo.grid(row=row, column=1, sticky="ew", padx=10, pady=7)
        combo.bind("<<ComboboxSelected>>", lambda _event: variable.set(value_by_label[shown.get()]))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        scroll = ScrollableFrame(self)
        scroll.grid(row=0, column=0, sticky="nsew")
        root = scroll.content
        root.columnconfigure(0, weight=1)

        paths = ttk.LabelFrame(root, text="Pastas")
        paths.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, "Pasta do Victoria 3", self.game, 0)
        self._path_row(paths, "Pasta de saída", self.output, 1)

        options = ttk.LabelFrame(root, text="Geração do mundo")
        options.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="Seed").grid(row=0, column=0, sticky="w", padx=10, pady=7)
        seed_frame = ttk.Frame(options)
        seed_frame.grid(row=0, column=1, sticky="ew", padx=10, pady=7)
        seed_frame.columnconfigure(0, weight=1)
        ttk.Entry(seed_frame, textvariable=self.seed).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            seed_frame,
            text="Nova seed",
            command=lambda: self.seed.set(str(random.randrange(1, 2_147_483_647))),
        ).grid(row=0, column=1, padx=(8, 0))

        rows = [
            ("Regiões estratégicas", self.strategic_regions, [
                ("Manter originais", "keep"),
                ("Recriar regiões contíguas e balanceadas — recomendado", "contiguous"),
                ("Recriar regiões contíguas por perfil econômico e físico", "economic"),
                ("Caos geográfico moderado, ainda contíguo", "chaos"),
            ]),
            ("Cores dos países", self.country_colors, [
                ("Manter cores do mod-base", "keep"),
                ("Aleatórias com contraste global", "global_contrast"),
                ("Contraste entre países vizinhos — recomendado", "neighbour_contrast"),
                ("Paleta vívida / alto contraste", "vivid"),
                ("Paleta suave, mas distinguível", "soft"),
            ]),
            ("Tamanho e quantidade dos países", self.country_scale, [
                ("Muitos países pequenos e fragmentados", "fragmented"),
                ("Mosaico equilibrado e compacto — recomendado", "balanced"),
                ("Menos países, maiores — semelhante ao jogo-base", "vanilla_like"),
                ("Poucos países e grandes blocos territoriais", "large_blocks"),
            ]),
            ("Distribuição inicial de potência", self.power_distribution, [
                ("Natural, sem regiões favorecidas", "natural"),
                ("Balanceada entre grandes zonas do mundo — recomendado", "balanced_continents"),
                ("Algumas potências regionais aleatórias", "regional_random"),
                ("Poucas grandes potências mundiais aleatórias", "global_random"),
                ("Manter estratégias regionais do mod-base", "keep_base"),
            ]),
            ("Recursos naturais", self.resources, [
                ("Manter originais", "keep"),
                ("Cinturões geológicos naturais + descobertas graduais — recomendado", "plausible_gradual"),
                ("Cinturões caóticos balanceados + descobertas graduais", "chaos_gradual"),
                ("Cinturões naturais, revelar tudo em 1836", "plausible_full"),
                ("Cinturões caóticos, revelar tudo em 1836", "chaos_full"),
            ]),
            ("Recursos visíveis em 1836", self.resource_visibility, [
                ("Muito poucos", "very_sparse"),
                ("Poucos — recomendado", "sparse"),
                ("Moderados", "moderate"),
            ]),
            ("Quantidade de terra arável", self.arable_land, [
                ("Manter original", "keep"),
                ("Redistribuir dentro de regiões", "regional"),
                ("Redistribuir globalmente", "global"),
            ]),
            ("Produtos agrícolas", self.arable_resources, [
                ("Manter originais", "keep"),
                ("Zonas agrícolas climáticas dentro de regiões", "regional_natural"),
                ("Zonas agrícolas climáticas globais — recomendado", "natural"),
                ("Caos agrícola global balanceado", "global_chaos"),
            ]),
            ("População", self.population, [
                ("Manter original", "keep"),
                ("Capacidade de sustentação dentro de regiões", "regional"),
                ("Capacidade de sustentação global — recomendado", "global"),
            ]),
            ("Culturas e homelands", self.cultures, [
                ("Manter mapa cultural original", "keep"),
                ("Blocos culturais contíguos e homelands coerentes — recomendado", "natural_blocks"),
            ]),
            ("Perfis tecnológicos e políticos", self.technology, [
                ("Manter diferenças originais", "keep"),
                ("Equalizar todos", "equalized"),
                ("Distribuição global natural, sem preferência europeia — recomendado", "natural_spread"),
            ]),
            ("Construções iniciais", self.buildings, [
                ("Manter originais", "keep"),
                ("Remover todas", "remove"),
                ("Arquétipos econômicos naturais e balanceados — recomendado", "balanced"),
            ]),
            ("Companhias", self.companies, [
                ("Manter companhias históricas", "keep"),
                ("Remover companhias iniciais", "remove"),
                ("Gerar poucas companhias iniciais balanceadas", "balanced_initial"),
                ("Companhias raras e dinâmicas conforme a economia — recomendado", "natural_dynamic"),
            ]),
            ("Necessidades estratégicas", self.strategic_needs, [
                ("Desativadas", "off"),
                ("Uma carência estrutural por arquétipo — recomendado", "natural"),
                ("Carências mais fortes e variadas", "strong"),
            ]),
            ("Diplomacia inicial", self.diplomacy, [
                ("Manter diplomacia histórica", "keep"),
                ("Neutralizar pactos e relações históricas", "clean"),
                ("Relações procedurais esparsas — recomendado", "natural_relations"),
                ("Relações procedurais + poucos pactos seguros", "natural_pacts"),
            ]),
            ("Territórios ultramarinos", self.overseas_territories, [
                ("Nenhum território ultramarino gerado", "none"),
                ("Raros domínios coloniais contíguos — recomendado", "rare_colonial"),
                ("Poucos domínios coloniais contíguos", "few_colonial"),
                ("Manter lógica ultramarina original do mod", "original"),
            ]),
            ("Vassalos e outros súditos", self.subjects, [
                ("Não gerar súditos", "none"),
                ("Raríssimos vassalos vizinhos e plausíveis — recomendado", "very_rare"),
                ("Poucos vassalos vizinhos e plausíveis", "rare"),
                ("Manter lógica original do mod", "original"),
            ]),
            ("Propriedade extraterritorial", self.foreign_investment, [
                ("Bloquear investimento estrangeiro automático — recomendado", "block_ai"),
                ("Manter as regras automáticas do jogo", "keep"),
            ]),
            ("Remanescentes de países históricos", self.historical_remnants, [
                ("Preservar todos os remanescentes históricos", "preserve"),
                ("Dissolver remanescentes periféricos — recomendado", "dissolve"),
                ("Transformar remanescentes em países procedurais locais", "procedural"),
                ("Permitir raríssimos governos históricos no exílio", "rare_exiles"),
            ]),
            ("Centros de civilização", self.civilization_centers, [
                ("Desativados", "off"),
                ("Centros globais raros e balanceados — recomendado", "sparse"),
                ("Mais centros globais", "abundant"),
            ]),
            ("Desenvolvimento inicial", self.development, [
                ("Muito baixo", "very_low"),
                ("Baixo", "low"),
                ("Normal", "normal"),
                ("Alto", "high"),
            ]),
            ("Segurança fiscal inicial", self.fiscal_safety, [
                ("Estabilidade máxima — recomendada", "strict"),
                ("Balanceada", "balanced"),
                ("Legado mais expansivo — risco de déficit", "legacy"),
            ]),
            ("Forças armadas, marinha e estaleiros", self.military_economy, [
                ("Não gerar forças militares iniciais", "none"),
                ("Avaliação econômica conservadora — recomendada", "economic_conservative"),
                ("Avaliação econômica balanceada", "economic_balanced"),
                ("Avaliação econômica permissiva", "economic_strong"),
            ]),
            ("Intensidade da variação", self.intensity, [
                ("Baixa", "low"),
                ("Média", "medium"),
                ("Alta", "high"),
            ]),
        ]
        for row_index, (label, variable, choices) in enumerate(rows, start=1):
            self._combo_row(options, label, variable, choices, row_index)

        note = ttk.Label(
            root,
            text=(
                "A geografia física é preservada. A v6.15 usa o provinces.png para regiões realmente contíguas e não inclui corrida por recursos nem crises regionais. "
                "Os modos completos de companhias e pactos dependem dos arquivos correspondentes na instalação atual do jogo."
            ),
            wraplength=900,
            justify="left",
        )
        note.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 2))

        self.log = tk.Text(root, height=11, wrap="word", state="disabled")
        self.log.grid(row=3, column=0, sticky="ew", padx=12, pady=8)

        buttons = ttk.Frame(root)
        buttons.grid(row=4, column=0, sticky="ew", padx=12, pady=10)
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="GERAR MOD", command=self._run).grid(row=0, column=1, padx=6)
        ttk.Button(buttons, text="Fechar", command=self.destroy).grid(row=0, column=2, padx=6)

    def _say(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    def _show_seed_preview(self, report_text: str) -> bool:
        result = {"save": False}
        window = tk.Toplevel(self)
        window.title("Panorama da seed — revisar antes de salvar")
        window.geometry("980x760")
        window.minsize(760, 560)
        window.transient(self)
        window.grab_set()
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)

        ttk.Label(
            window,
            text=(
                "Revise o panorama desta seed. O mod só será copiado para a pasta de saída "
                "e compactado depois que você clicar em SALVAR MOD."
            ),
            wraplength=920,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

        frame = ttk.Frame(window)
        frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.insert("1.0", report_text)
        text.configure(state="disabled")

        buttons = ttk.Frame(window)
        buttons.grid(row=2, column=0, sticky="ew", padx=14, pady=14)
        buttons.columnconfigure(0, weight=1)

        def accept() -> None:
            result["save"] = True
            window.destroy()

        def reject() -> None:
            result["save"] = False
            window.destroy()

        ttk.Button(buttons, text="CANCELAR E DESCARTAR SEED", command=reject).grid(row=0, column=1, padx=6)
        ttk.Button(buttons, text="SALVAR MOD", command=accept).grid(row=0, column=2, padx=6)
        window.protocol("WM_DELETE_WINDOW", reject)
        self.wait_window(window)
        return bool(result["save"])

    def _run(self) -> None:
        try:
            seed = int(self.seed.get().strip())
            game = Path(self.game.get().strip())
            output = Path(self.output.get().strip())
            output.mkdir(parents=True, exist_ok=True)
            options = {
                "resources": self.resources.get(),
                "resource_visibility": self.resource_visibility.get(),
                "arable_land": self.arable_land.get(),
                "arable_resources": self.arable_resources.get(),
                "population": self.population.get(),
                "cultures": self.cultures.get(),
                "buildings": self.buildings.get(),
                "technology": self.technology.get(),
                "development": self.development.get(),
                "fiscal_safety": self.fiscal_safety.get(),
                "military_economy": self.military_economy.get(),
                "intensity": self.intensity.get(),
                "strategic_regions": self.strategic_regions.get(),
                "country_colors": self.country_colors.get(),
                "country_scale": self.country_scale.get(),
                "power_distribution": self.power_distribution.get(),
                "companies": self.companies.get(),
                "strategic_needs": self.strategic_needs.get(),
                "diplomacy": self.diplomacy.get(),
                "civilization_centers": self.civilization_centers.get(),
                "overseas_territories": self.overseas_territories.get(),
                "subjects": self.subjects.get(),
                "historical_remnants": self.historical_remnants.get(),
                "foreign_investment": self.foreign_investment.get(),
            }
            self._say(f"Gerando seed {seed}...")
            _root, install_zip, validation = generate_world(
                game,
                BASE / "template_mod",
                output,
                options,
                seed,
                preview_callback=self._show_seed_preview,
            )
            for line in validation:
                self._say(line)
            self._say(f"Concluído: {install_zip}")
            messagebox.showinfo(
                "Concluído",
                f"Mod gerado com sucesso:\n{install_zip}\n\nAtive apenas esta versão no launcher.",
            )
        except GenerationCancelled as exc:
            self._say(str(exc))
            messagebox.showinfo("Seed descartada", "A seed foi descartada. Nenhum mod foi salvo.")
        except Exception as exc:
            self._say("ERRO: " + str(exc))
            self._say(traceback.format_exc())
            messagebox.showerror("Erro", str(exc))


if __name__ == "__main__":
    App().mainloop()
