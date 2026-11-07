# Política de segurança

## Versões suportadas

Só a versão mais recente recebe correções de segurança. Atualiza sempre para a
última versão publicada antes de reportar um problema.

## Como reportar uma vulnerabilidade

**Não abras uma issue pública nem a comentes num grupo.** Usa o reporte privado
do GitHub: separador **Security** → **Report a vulnerability**
(<https://github.com/CarlosAndreFerreira/moodle-obsidian-mcp/security/advisories/new>).

Inclui, se possível:

- a versão (`moodle-mcp --version`) e o sistema operativo;
- os passos para reproduzir, ou uma prova de conceito **com tokens falsos**;
- o impacto que vês (que dados ou que conta ficam expostos).

Recebes uma resposta inicial em até 7 dias. Depois de confirmada, a correção é
publicada numa nova versão e o problema é divulgado num *security advisory*,
com crédito a quem o reportou (se o quiseres).

## O que conta como vulnerabilidade

O servidor guarda o `wstoken` do Moodle de um estudante e lê conteúdo escrito
por terceiros (fóruns, páginas). Interessam sobretudo problemas que permitam:

- **Exfiltração do token** ou de dados da conta Moodle para outro anfitrião.
- **Ações no Moodle sem confirmação** do estudante (mensagens, posts,
  submissões), por exemplo através de *prompt injection*.
- **Leitura ou escrita fora do Vault** do Obsidian.
- **Troca do token guardado** pelo de outra conta (p.ex. pelo handler
  `moodlemobile://`).
- **Execução de código** a partir de notas escritas no Vault.

Fora de âmbito: vulnerabilidades do próprio Moodle ou do Obsidian, e ataques que
exijam já ter acesso à conta do utilizador no computador.

## Defesas existentes

Para contexto de quem reporta, o README descreve as defesas em vigor:
confinamento ao Vault, token só enviado para ficheiros da instância configurada,
redação de segredos nas respostas, limpeza de conteúdo escondido, confirmação
humana das ações no Moodle (elicitation), login com *passport* de uso único,
ferramentas de escrita desligadas por omissão e registo de auditoria.
