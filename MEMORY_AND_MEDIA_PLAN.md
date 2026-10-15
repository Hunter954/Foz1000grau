# Foz1000Grau: plano e implementação de redução de custos

## Diagnóstico de produção

Baseline coletada em 15/10/2026, últimas 24 horas, amostras a cada 60 segundos:

| Serviço | RAM média | RAM atual | Volume atual |
| --- | ---: | ---: | ---: |
| web | 0,412 GB | 0,415 GB | 1,667 GB |
| Postgres | 0,208 GB | 0,215 GB | 1,418 GB |

Causas encontradas no código: dois processos Gunicorn; painel editorial sem paginação; estatísticas de sessões carregadas como objetos Python; recálculo do dashboard em telas que não usam seus dados; varredura e ordenação de todo o volume em cada biblioteca/dashboard; imagens originais salvas sem compressão; recebimento do espelho bloqueado por download remoto; imagens do corpo do espelho sem tratamento.

## Implementado

1. Gunicorn com um processo e quatro threads; pool PostgreSQL de quatro conexões e dois overflows (até seis conexões para quatro threads HTTP, o consumidor e uma transação curta de mídia). Driver psycopg2 explícito e SQLAlchemy fixado, evitando mudança de driver durante rebuild.
2. Agregação de analytics em SQL, paginação editorial de 40 itens, biblioteca de 48 itens com catálogo SQL. Consultas de listagem usam resumo de até 1.000 caracteres em lugar do corpo completo.
3. Fotos estáticas em WebP, lado máximo de 1.280 px, qualidade 68, uma codificação por imagem, sem busca repetitiva de qualidade. Orientação EXIF corrigida e metadados removidos. Capas JPEG/PNG também podem ser reduzidas no navegador antes do envio.
4. Arquivos de entrada limitados a 15 MB e 24 megapixels. Cópia em blocos para arquivo temporário; processamento de pixels serializado. GIF/WebP animado preserva animação; SVG mantém o vetor. Esses casos não recebem compressão estática com perda.
5. Nomes derivados do conteúdo evitam duplicatas das fotos comprimidas. Cache persistente por URL evita baixar novamente a mesma imagem importada.
6. Matéria do espelho é gravada primeiro e retorna imediatamente. Capa e imagens do corpo são processadas em segundo plano. A origem permanece visível até o resultado existir; atualização compara os valores anteriores para não sobrescrever edição posterior. `srcset` antigo é removido no processamento para não continuar servindo a foto original.
7. Fila no banco, consumidor único protegido por lock de arquivo entre processos/deploys, tentativas espaçadas, cinco tentativas totais e estado de falha consultável. Um trabalho por vez, com prioridade para publicações/uploads novos sobre a migração histórica, sem contratar outro serviço/worker.
8. Migração automática e serial de arquivos antigos, sem apagar originais. Primeiro pode gerar WebP local; quando R2 é configurado, envia versões otimizadas e mantém os links antigos como redirecionamentos. HTML público passa a usar o destino resolvido diretamente.
9. Upload R2 sem paralelismo de transferências, cliente reutilizado, nomes imutáveis e `Cache-Control: public, max-age=31536000, immutable`. Não há proxy de bytes de imagens R2 pelo Railway.
10. `/health` informa otimização/R2; `/admin/api/resources` exige administrador e mostra fila e contadores de mídia.

## Ativação do Cloudflare — pendente de autenticação

O painel retornou `Unable to sign in. Try again or use your email and password.` após a tentativa de login com Google. Nenhum bucket ou credencial foi criado. O código funciona com armazenamento local enquanto o acesso não é resolvido.

Após acesso à conta correta:

1. Criar bucket exclusivo `foz1000grau-media`, armazenamento Standard.
2. Conectar domínio público próprio, por exemplo `imagens.foz1000grau.com.br`, com cache Cloudflare. Validar disponibilidade HTTPS antes de ativar o destino no site. `r2.dev` é destinado a desenvolvimento e tem limitação de requisições.
3. Usar credencial limitada a leitura/gravação dos objetos desse bucket; configurar como segredo no Railway, sem enviar senhas ou chaves no chat.
4. Definir no serviço web: `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_URL`. Apenas o conjunto completo habilita R2. Novo deploy inicia a migração do volume para R2.
5. Conferir foto pública, Content-Type, cache, URLs diretas no HTML, progresso da fila e custos. Só considerar remoção dos originais/volume após validar a migração e a cobertura de links antigos. Esta implementação os preserva deliberadamente.

R2 não cobra egress para internet, mas armazenamento e operações podem ser cobrados. Compressão e cache reduzem bytes, tempo de carregamento e requisições ao armazenamento. Não há garantia de custo zero.

Referências oficiais: https://developers.cloudflare.com/r2/pricing/ e https://developers.cloudflare.com/r2/buckets/public-buckets/.

## Operação

- `flask --app wsgi migrate-media`: cataloga arquivos existentes; o worker de produção já faz isso automaticamente.
- `MEDIA_WORKER_ENABLED=0 flask --app wsgi migrate-media --process`: alternativa manual, sem concorrer com o consumidor automático.
- `flask --app wsgi retry-media`: recoloca trabalhos falhos na fila após corrigir a causa.
- `MEDIA_WORKER_ENABLED=0`: pausa consumo sem perder a fila e sem interromper publicação.
- A remoção da biblioteca oculta a entrada e preserva arquivos compartilhados/referenciados.

## Validação

21 testes de regressão: compressão/tamanho/deduplicação, limites de bytes/pixels, animação, isolamento da transação editorial, upload assíncrono R2, URL direta/redirect/cache, migração local e ativação posterior do R2, preservação dos originais, tentativas persistentes, capa/corpo do espelho, paginação/páginas administrativas, analytics SQL, retorno imediato do espelho, driver PostgreSQL, reenvio durante processamento, corpo de matéria não carregado em listagens, download remoto limitado/cacheado e proteção de edições concorrentes e prioridade das novas publicações e Content-Type WebP mesmo em runtimes sem o registro MIME.

A redução de RAM/custo deve ser comparada durante 24–48 horas de tráfego semelhante. O consumo inicial pode incluir migração. O banco mantém sua configuração de memória: não foi imposto um teto artificial que pudesse derrubar o serviço.
