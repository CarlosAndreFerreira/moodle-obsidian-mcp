# 🎓 Moodle-Obsidian Doctoral MCP & SSO Auto-Token Suite

Servidor **Model Context Protocol (MCP)** em Python (`FastMCP`) desenhado para estudantes de Doutoramento / Ensino Superior em plataformas **Moodle** com **Single Sign-On (SSO)** federado (como a **Universidade Aberta — `elearning.uab.pt`** com credenciais **UTAD / RCTSaai** e **`e-learning.utad.pt`**), integrado diretamente com o **Obsidian**.

---

## ✨ Funcionalidades Principais

1. **Auto-Descoberta de Token Mobile via Browser (`moodlemobile://`):**
   * Mesmo quando a universidade usa SSO federado (Microsoft Entra ID / SAML / Shibboleth / CAS) e oculta a página `/user/managetoken.php` aos estudantes, o capturador nativo (`capture_mobile_token.py` registado via `xdg-mime`) interceta o redirecionamento `moodlemobile://token=...` diretamente do navegador (Brave, Firefox, Chrome), descodifica o Base64, guarda o `wstoken` em `.env` e sincroniza o cofre do Obsidian automaticamente.
2. **Sincronização Completa para o Obsidian (`sync_doctoral_dashboard_to_obsidian`):**
   * Gera um painel central (`Doutoramento - Painel Moodle.md`) com tabela de Unidades Curriculares, progresso, notas atuais e calendário ordenado de prazos de *e-fólios* / trabalhos.
   * Cria uma nota individual por cada Unidade Curricular (`Unidades Curriculares/<Nome da UC>.md`) com grelha de avaliação (`gradereport`), estado de submissão de cada *e-fólio* (`mod_assign`), enunciados convertidos para Markdown e árvore de recursos.
3. **Download de Materiais + Extração de PDFs para Markdown (`PyMuPDF`):**
   * `download_all_course_materials_to_obsidian`: Descarrega automaticamente todos os PDFs, slides, enunciados anexos e páginas HTML de uma cadeira para `Unidades Curriculares/<UC>/Materiais/` e converte cada PDF num ficheiro `.pdf.md` pesquisável no Obsidian e legível por agentes de IA.
4. **32 Ferramentas MCP para Estudantes (Leitura + Escrita):**
   * **Cursos, Livros e Progresso:** `get_enrolled_courses`, `get_course_contents`, `get_course_pages_and_books` (`mod_book`, `mod_page`, `mod_folder`, `mod_resource`), `get_activities_completion_status`, `toggle_activity_completion`, `get_course_participants`.
   * **Notas, Trabalhos e Submissões:** `get_course_grades`, `get_all_grades_overview`, `get_assignments`, `get_assignment_submission`, `get_course_quizzes`, `upload_file_to_draft`, `save_assignment_submission`, `add_submission_comment`.
   * **Fóruns Assíncronos e Mensagens:** `get_course_forums`, `get_forum_discussions`, `get_discussion_posts`, `search_all_course_forums`, `export_forum_discussions_to_obsidian`, `reply_to_forum_post`, `create_forum_discussion`, `send_instant_message`, `get_messages`, `get_calendar_events`.
   * **Suporte Multi-Instância:** Parâmetro `instance="uab"` ou `instance="utad"` em todas as ferramentas.

---

## 🚀 Instalação Rápida (Linux)

```bash
git clone git@github.com:CarlosAndreFerreira/moodle-obsidian-mcp.git
cd moodle-obsidian-mcp
bash install.sh ~/Doutoramento
```

### Autenticação Automática via Browser (1 Clique)
1. Faz login normalmente no Moodle da tua universidade no navegador (ex.: `https://elearning.uab.pt` com conta UTAD/federada).
2. Abre no mesmo navegador:
   ```text
   https://elearning.uab.pt/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345
   ```
3. Aceita o pop-up **"Abrir Moodle Mobile Token Catcher"** — o token é extraído, guardado localmente no `.env` (ignorado pelo Git) e o teu cofre do Obsidian é sincronizado de imediato!

---

## 🔒 Segurança e Privacidade
* O ficheiro `.gitignore` bloqueia estritamente `.env`, `*.log` e quaisquer ficheiros de credenciais/tokens.
* O script `capture_mobile_token.py` nunca regista tokens em ficheiros de log.
