# Randomised World — World History Generator v6.15

A v6.15 preserva a economia jogável da v6.14 e acrescenta a camada comercial que faltava. Cada país centralizado recebe um pequeno centro comercial; portos costeiros começam no método Porto Básico e produzem Marinha Mercante; uma parcela economicamente apta dos países costeiros recebe um estaleiro civil com construção naval básica. A oferta de alimentos, ferramentas, tecidos e móveis também passa a crescer com o tamanho do país, reduzindo colapsos generalizados de padrão de vida sem eliminar carências iniciais.


A v6.14 corrige a regressão em que os países surgiam apenas com um cais e poucas fazendas. O gerador cria primeiro um piso econômico funcional: alimento em estados populosos, madeira, pecuária, pesca e minas onde existe potencial real, além de um pequeno núcleo industrial, administrativo e de construção no capital. Somente depois são avaliados quartéis, estaleiros, administrações navais e forças militares. Nenhuma região histórica recebe multiplicador.

Correção geográfica da v6 para Victoria 3 1.13.*.

A v6 usava uma aproximação baseada na ordem dos arquivos para estimar quais estados eram vizinhos. Essa aproximação podia considerar estados distantes como adjacentes e produzir regiões estratégicas fragmentadas. A v6.1 remove completamente esse método.

## Correção principal

Quando qualquer modo procedural de regiões estratégicas é selecionado, o gerador agora lê diretamente:

`Victoria 3/game/map_data/provinces.png`

A imagem de províncias é cruzada com as cores listadas nos arquivos de `map_data/state_regions`. O gerador identifica fronteiras reais entre estados pixel a pixel e acrescenta os estreitos definidos em `common/strait_definitions`.

Com isso:

- regiões em uma mesma massa continental são formadas apenas por estados que realmente dividem fronteira;
- estados não podem aparecer como enclaves distantes dentro da mesma região;
- componentes insulares são mantidos inteiros;
- ilhas são anexadas à região que herdou sua antiga área geográfica ou, na falta dela, à região mais próxima;
- a geração é interrompida se `provinces.png` não estiver disponível, em vez de produzir uma aproximação defeituosa.

## Instalação

1. Extraia todo o ZIP.
2. Execute `INICIAR_GERADOR.bat`.
3. Selecione a pasta completa do Victoria 3, normalmente:
   `Steam/steamapps/common/Victoria 3`
4. Escolha a pasta de saída:
   `Documentos/Paradox Interactive/Victoria 3/mod`
5. Escolha as opções e clique em **GERAR MOD**.

Para aleatorizar regiões estratégicas, não selecione uma cópia parcial da pasta `game`: ela precisa conter `map_data/provinces.png`.

## Modos de regiões

- **Manter originais:** não exige `provinces.png`.
- **Contíguas e balanceadas:** usa fronteiras reais e busca áreas compactas.
- **Por perfil econômico e físico:** mantém contiguidade real, mas posiciona sementes em polos econômicos diferentes.
- **Caos moderado:** varia mais as sementes e os tamanhos, sem quebrar a contiguidade.

## Demais módulos

Foram preservados os sistemas da v6:

- cinturões geológicos e reservas descobríveis;
- zonas agrícolas climáticas;
- população por capacidade de sustentação;
- blocos culturais e homelands;
- tecnologia e política sem preferência automática pela Europa;
- arquétipos econômicos e construções concentradas;
- companhias raras e dinâmicas;
- necessidades estratégicas;
- diplomacia procedural esparsa;
- centros de civilização;
- história procedural da seed.

Não foram incluídos módulos de corrida por recursos nem crises regionais.

## Validação

O gerador verifica:

- cada estado atribuído exatamente uma vez;
- nenhuma região vazia;
- capital pertencente à própria região;
- conexão de todos os estados da região na malha territorial e nos vínculos insulares controlados;
- integridade de chaves e arquivos gerados.

O teste definitivo continua sendo a abertura de uma nova campanha no Victoria 3.

## Correção de idioma da v6.5

As regiões estratégicas reutilizam chaves do jogo-base. Por isso, a localização precisa ser gravada na pasta especial de substituição:

`localization/<idioma>/replace/`

A v6.5 gera os nomes nesse local para todos os idiomas encontrados na instalação e usa o nome localizado do estado-capital em cada idioma. Em português brasileiro, por exemplo, o formato passa a ser **[nome localizado do estado]**.

Mundos novos já recebem essa correção automaticamente. Para corrigir um mundo criado pela v6.1 sem gerar tudo novamente, execute:

`CORRIGIR_LOCALIZACAO_REGIOES.bat`

Selecione primeiro a instalação do Victoria 3 e depois a pasta do mundo gerado dentro de `Documentos/Paradox Interactive/Victoria 3/mod`. Feche completamente o jogo antes de aplicar a correção.

## Territórios ultramarinos e súditos

A v6.5 adiciona opções independentes para limitar a formação política inicial:

- **Territórios ultramarinos:** nenhum; raros e compactos; poucos e compactos; ou lógica original. Nos modos controlados, somente poucos países costeiros grandes podem receber um domínio colonial, formado por um estado costeiro e, ocasionalmente, um ou dois estados vizinhos do mesmo antigo proprietário. Todo o domínio começa não incorporado.
- **Vassalos e outros súditos:** nenhum; raríssimos; poucos; ou lógica original. Nos modos controlados, existe um teto mundial e só um país muito pequeno pode tornar-se vassalo de um vizinho bem maior e de cultura primária diferente. Uniões pessoais aleatórias não são criadas.

Os nomes procedurais das regiões estratégicas exibem somente o nome localizado do estado-capital, sem prefixos como “Região Estratégica de”.

## Cores dos países — v6.5

A v6.5 acrescenta uma etapa própria de coloração política. O objetivo não é apenas sortear cores diferentes, mas impedir que países vizinhos recebam tons quase idênticos.

Modos disponíveis:

- **Manter cores do mod-base:** não altera a coloração existente.
- **Contraste global:** utiliza uma paleta perceptualmente espaçada, sem exigir a malha de fronteiras.
- **Contraste entre países vizinhos — recomendado:** lê `map_data/provinces.png`, monta a malha real de estados e atribui as cores por um algoritmo de coloração de grafo.
- **Paleta vívida:** aplica a mesma proteção de fronteiras, com maior saturação.
- **Paleta suave:** mantém tons mais claros, mas ainda exige separação perceptual entre vizinhos.

A cor principal de cada país procedural é determinada pelo seu estado-capital. Como os países criados pelo randomizador normalmente surgem a partir de um estado, esse método dá contraste direto às fronteiras iniciais e evita blocos inteiros de rosa, lilás ou azul-claro.

Além disso:

- nenhum par de estados vizinhos pode receber exatamente a mesma cor nos modos geográficos;
- estados a dois passos de distância também influenciam a escolha, evitando uma região inteira presa à mesma família cromática;
- países sujeitos aparecem com uma variante mais escura da cor do próprio estado-capital;
- territórios coloniais pertencentes à metrópole mantêm naturalmente a cor da metrópole;
- as cores literais usadas como fallback em `create_dynamic_country` também são substituídas pela nova paleta;
- definições nacionais auxiliares do mod são recoloridas conforme o estado-capital.

Cada mundo gera o relatório `COUNTRY_COLOR_REPORT_PT-BR.txt`, com a distância perceptual mínima e média nas fronteiras, a quantidade de cores utilizadas e a confirmação de que não existem vizinhos com cores idênticas.

Os modos **contraste entre vizinhos**, **vívido** e **suave** exigem a instalação completa do jogo com `map_data/provinces.png`.

### Corrigir apenas as cores de um mundo já gerado

A pasta também inclui `CORRIGIR_CORES_PAISES.bat`. Esse utilitário:

1. cria `BACKUP_CORES_ANTES_V6_4.zip` dentro da pasta do mundo;
2. recalcula as cores usando a instalação completa do Victoria 3;
3. substitui somente os arquivos de cor e os fallbacks nacionais;
4. não altera fronteiras, população, recursos, construções, colônias ou súditos.

Feche completamente o Victoria 3 antes de executar a correção.

## Tamanho e quantidade dos países — v6.5

A v6.5 acrescenta uma opção própria para controlar o grau de fragmentação política.
Ela atua antes das colônias e dos vassalos, modificando a quantidade de novos países
criados em cada área do mundo e o número de estados contíguos cedidos a cada país.

Modos disponíveis:

- **Muitos países pequenos e fragmentados:** mantém aproximadamente a intensidade do randomizador original e cria, em geral, países de um estado.
- **Mosaico equilibrado e compacto — recomendado:** reduz as tentativas de criação de países para cerca de 65% e permite núcleos contíguos de até três estados.
- **Menos países, maiores — semelhante ao jogo-base:** reduz as tentativas para cerca de 38% e forma núcleos contíguos de até quatro estados.
- **Poucos países e grandes blocos territoriais:** reduz as tentativas para cerca de 20% e tenta formar blocos contíguos de até seis estados.

Os estados adicionais só podem ser vizinhos e pertencer ao mesmo proprietário anterior.
Assim, um novo país recebe um bloco territorial conectado, e não estados sorteados em
pontos diferentes do mapa. O total exato de países continua variando conforme a seed e
a disponibilidade de estados elegíveis durante a inicialização.

Cada mundo inclui `COUNTRY_SCALE_REPORT_PT-BR.txt`, que registra a faixa teórica de
criação de países e o tamanho máximo de bloco selecionado. Alterar essa opção exige
gerar um mundo novo e iniciar uma campanha nova.


## Distribuição de potência — v6.6

A v6.8 remove, nos modos controlados, o laço exclusivo que fragmentava pouco as Ilhas Britânicas e também desativa as estratégias de reunificação fixadas às antigas regiões históricas. Isso evita que a mesma localização produza repetidamente a principal superpotência.

O menu oferece:

- **Natural, sem regiões favorecidas:** nenhum bônus artificial; a ascensão depende apenas da economia e da geografia da seed.
- **Balanceada entre grandes zonas do mundo — recomendado:** cada grande zona recebe um polo promissor moderado e temporário.
- **Algumas potências regionais aleatórias:** vários polos são sorteados globalmente, com limite de concentração por zona.
- **Poucas grandes potências mundiais aleatórias:** poucos polos recebem um impulso inicial maior, ainda temporário.
- **Manter estratégias regionais do mod-base:** restaura o comportamento anterior, inclusive as estratégias históricas especiais.

Os impulsos duram vinte anos e são deliberadamente moderados. Eles aumentam prestígio, inovação, burocracia e, nos modos mais fortes, influência. Não concedem território, recursos ou tecnologias incompatíveis.

## Panorama da seed antes de salvar

A geração agora ocorre em uma pasta temporária. Antes de o mod ser copiado para a pasta de saída e compactado, o gerador abre uma janela com `PANORAMA_DA_SEED_PT-BR.txt`. O relatório apresenta:

- as regiões materialmente mais favorecidas;
- a concentração mundial de população, terra arável e recursos;
- os polos de potência escolhidos pela seed;
- os principais centros demográficos, agrícolas, industriais, marítimos e de reservas futuras;
- a fragmentação política esperada;
- ultramar, súditos e diplomacia selecionados;
- uma interpretação histórica procedural do mundo.

Ao clicar em **Cancelar e descartar seed**, a pasta temporária é apagada e nenhum mod é salvo. Ao clicar em **Salvar mod**, o mod é transferido para a pasta de saída e o ZIP de instalação é criado.

O relatório consegue descrever exatamente a geografia estratégica, os recursos, a população e os polos escritos nos arquivos. Os países dinâmicos são formados pelo motor do Victoria 3 ao iniciar a campanha; portanto, a quantidade final de países é apresentada como faixa de tentativas do script, não como uma captura de uma campanha já inicializada.


## Segurança fiscal e propriedade local — v6.8

A geração balanceada de construções agora segue uma regra rígida: **nenhuma construção inicial é criada em território estrangeiro ou em estado ultramarino não incorporado**. Os hubs rural, mineral, industrial e costeiro são escolhidos somente entre estados incorporados pertencentes ao país processado.

A opção **Segurança fiscal inicial** possui três níveis:

- **Conservadora — recomendada:** garante um núcleo civil pequeno e só concede forças permanentes a países que tenham população e cadeia doméstica suficientes.
- **Balanceada:** amplia moderadamente agricultura, extração, indústria e limites militares.
- **Legado mais expansivo:** cria uma base econômica maior e aceita mais pressão fiscal; existe para mundos mais desenvolvidos.

O bloco antigo que favorecia regiões históricas e criava forças apenas por tecnologia e litoral foi removido. A v6.14 pode criar pequenos exércitos e frotas, mas somente depois de verificar alimentos processados, ferramentas, recursos, porto, população e território.

O relatório da seed registra o perfil fiscal selecionado e a validação confirma que o script gerado contém as salvaguardas `is_incorporated = yes` e `owner = scope:bwg_country`.


## Remanescentes de países históricos (v6.8)

O gerador agora identifica o núcleo territorial inicial de cada país a partir da malha real de estados. Quando uma tag histórica perde integralmente esse núcleo e sobrevive apenas em possessões desconectadas — por exemplo, Portugal nos Açores ou a Espanha nas Canárias — ela deixa de ser preservada automaticamente.

Modos disponíveis:

- **Preservar:** mantém o comportamento antigo.
- **Dissolver — recomendado:** tenta anexar o remanescente a um país procedural vizinho; quando a possessão é uma ilha sem vizinho adequado, cria um sucessor procedural local.
- **Procedural:** sempre substitui a antiga tag por um país local procedural.
- **Raros governos no exílio:** permite que aproximadamente um em cada oito remanescentes históricos sobreviva.

Países legitimamente insulares não são apagados enquanto conservarem parte de seu núcleo territorial original.


## Hotfix v6.8.1 — criação de súditos

A v6.8 procurava apenas o comentário original `PRE-EXISTING SUBJECTS` para inserir a limpeza de remanescentes históricos. Nos modos recomendados de súditos, esse bloco já havia sido substituído por `CONTROLLED PRE-EXISTING SUBJECTS` ou `PRE-EXISTING SUBJECTS DISABLED`, fazendo a geração parar antes de salvar.

A v6.8.1 cria um marcador interno estável antes da seção de súditos e também reconhece todas as variantes antigas. A correção funciona com: manter lógica original, nenhum súdito, raríssimos súditos e poucos súditos.


## v6.14 — Economia local, infraestrutura e propriedade

A v6.14 substitui o antigo sistema de três hubs gigantes por uma geração **estado a estado**. Fazendas de alimento são calculadas pela população local; extração, plantações, pesca e madeira possuem tetos baixos; indústria e prédios públicos ficam concentrados em apenas um núcleo nacional. Países pequenos podem começar com carências, mas o gerador não deve consumir o dobro da infraestrutura disponível nem criar estruturas públicas incompatíveis com a população.

A limpeza de companhias e recipientes históricos de propriedade ocorre **antes** de remover e recriar edifícios. A auditoria de países históricos sem núcleo territorial também é executada novamente nesse momento, corrigindo casos como a França sobrevivendo apenas na Córsega.

A nova opção **Propriedade extraterritorial** permite bloquear o investimento estrangeiro automático da IA. Nesse modo, os pesos `foreign_investment_ai_factor` são neutralizados, impedindo que minas e fábricas estrangeiras isoladas reapareçam nos primeiros meses. Ações manuais e tratados explícitos de investimento continuam possíveis durante a campanha.


## v6.14 — Forças militares calculadas pela economia

A geração balanceada remove exércitos e marinhas históricos e, somente depois de criar a economia civil, avalia cada país. Quartéis, administrações navais, estaleiros e navios exigem população incorporada, tecnologia, litoral e cadeias domésticas compatíveis. Não existe bônus especial para Inglaterra ou outra região histórica.

O modo conservador normalmente deixa microestados sem forças permanentes e limita países aprovados a poucos níveis. A reconstrução automática usa apenas fragatas; navios capitais continuam a cargo da campanha.


## v6.14 — reconstrução compatível da economia

A v6.14 corrige a regressão em que a remoção histórica funcionava, mas a reconstrução econômica não era executada. A geração voltou ao padrão de escopo país → estado já utilizado pelo randomizador-base e deixou de calcular a população nacional por um agregador numérico incompatível. A opção escolhida no programa é gravada diretamente no efeito da seed e não depende mais de uma regra alterável no lobby.


## v6.14 — reconstrução econômica jogável

A v6.14 corrige o perfil excessivamente vazio da v6.13. Todos os países centralizados recebem somente o piso tecnológico necessário para uma economia de 1836 funcionar. Cada estado populoso tenta criar alimento, madeira, pecuária, pesca e extração compatíveis com seu potencial real; o capital recebe um núcleo pequeno de alimentos processados, ferramentas, administração e construção. Minas e plantações começam em nível baixo, sem multiplicadores regionais. Exércitos e marinhas continuam limitados pela população, pelo território e pelas cadeias domésticas, mas países aprovados recebem as tecnologias institucionais necessárias para que a criação não falhe silenciosamente.
