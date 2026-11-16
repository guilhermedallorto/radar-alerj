import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-only-placeholder")
os.environ.setdefault("GEMINI_API_KEY", "test-only-placeholder")

from worker.ingest import analyze_edition_for_users, find_part_ii_link, find_part_ii_links, publication_key, user_theme_signature


class EditionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.base_url = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_seleciona_edicao.php?data=example"
        self.html = """
        <!doctype html>
        <html><body>
          <a href="mostra_edicao.php?session=EXEC">Parte I (Poder Executivo)</a>
          <a href="mostra_edicao.php?session=LEG">Parte II (Poder Legislativo)</a>
          <a href="mostra_edicao.php?session=EXTRA">Parte II (Poder Legislativo) EDIÇÃO EXTRA</a>
          <a href="mostra_edicao.php?session=EXTRA2">Parte II (Poder Legislativo) EDIÇÃO EXTRA</a>
        </body></html>
        """

    def test_regular_link_is_not_confused_with_extra_or_executive(self):
        result = find_part_ii_link(self.html, self.base_url, "regular")
        self.assertTrue(result.endswith("session=LEG"))

    def test_extra_link_is_selected_separately(self):
        result = find_part_ii_link(self.html, self.base_url, "extra")
        self.assertTrue(result.endswith("session=EXTRA"))

    def test_all_extra_links_are_returned_without_collapsing_same_day_editions(self):
        results = find_part_ii_links(self.html, self.base_url, "extra")
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].endswith("session=EXTRA"))
        self.assertTrue(results[1].endswith("session=EXTRA2"))

    def test_absent_extra_returns_none_in_optional_mode(self):
        html_without_extra = self.html.replace(
            '<a href="mostra_edicao.php?session=EXTRA">Parte II (Poder Legislativo) EDIÇÃO EXTRA</a>',
            "",
        ).replace(
            '<a href="mostra_edicao.php?session=EXTRA2">Parte II (Poder Legislativo) EDIÇÃO EXTRA</a>',
            "",
        )
        result = find_part_ii_link(html_without_extra, self.base_url, "extra", required=False)
        self.assertIsNone(result)

    def test_user_theme_signature_is_independent_of_input_order(self):
        themes = [
            {"id": "b", "name": "Saúde", "description": "", "keywords": [], "include_criteria": "", "exclude_criteria": "", "priority": 2, "active": True, "updated_at": "2026-11-16T10:00:00+00:00"},
            {"id": "a", "name": "Educação", "description": "", "keywords": [], "include_criteria": "", "exclude_criteria": "", "priority": 1, "active": True, "updated_at": "2026-11-16T10:00:00+00:00"},
        ]
        publications = [
            {"id": "p2", "page_start": 2, "updated_at": "2026-11-16T10:00:00+00:00"},
            {"id": "p1", "page_start": 1, "updated_at": "2026-11-16T10:00:00+00:00"},
        ]
        first = user_theme_signature(themes, publications)
        second = user_theme_signature(list(reversed(themes)), list(reversed(publications)))
        self.assertEqual(first, second)

    def test_user_theme_signature_changes_when_active_theme_changes(self):
        themes = [{"id": "a", "name": "Educação", "active": True, "priority": 1, "updated_at": "2026-11-16T10:00:00+00:00"}]
        publications = [{"id": "p1", "page_start": 1, "updated_at": "2026-11-16T10:00:00+00:00"}]
        original = user_theme_signature(themes, publications)
        changed = user_theme_signature([{**themes[0], "name": "Educação pública"}], publications)
        self.assertNotEqual(original, changed)

    def test_extra_analysis_is_stored_separately_for_each_user(self):
        themes = [
            {"id": "u1-theme", "user_id": "user-1", "name": "Saúde", "description": "", "keywords": ["SUS"], "include_criteria": "", "exclude_criteria": "", "priority": 1, "active": True, "updated_at": "2026-11-16T10:00:00+00:00"},
            {"id": "u2-theme", "user_id": "user-2", "name": "Educação", "description": "", "keywords": ["escola"], "include_criteria": "", "exclude_criteria": "", "priority": 1, "active": True, "updated_at": "2026-11-16T10:00:00+00:00"},
        ]
        publications = [
            {"id": "pub-1", "edition_id": "edition-1", "page_start": 1, "page_end": 1, "kind": "Ato", "title": "Ato 1", "body_text": "Texto da publicação", "organization": "ALERJ", "updated_at": "2026-11-16T10:00:00+00:00"},
        ]
        writes = []

        def fake_get_all(table, params, page_size=1000):
            return themes if table == "themes" else publications

        def fake_gemini(prompt, *args, **kwargs):
            if "PUBLICAÇÕES DO LOTE:" in prompt:
                return {"items": [{"publication_id": "pub-1", "relevant": True, "matched_themes": ["Saúde", "Educação"], "confidence": 0.9, "justification": "Relação objetiva", "summary": "Resumo do ato"}]}
            return {"summary": "Síntese personalizada"}

        def fake_post(table, rows, on_conflict=None):
            writes.append((table, rows))
            return [{"id": "analysis-1"}]

        with patch("worker.ingest.db_get_all", side_effect=fake_get_all), \
             patch("worker.ingest.db_get", return_value=[]), \
             patch("worker.ingest.db_post", side_effect=fake_post), \
             patch("worker.ingest.db_patch", return_value=None), \
             patch("worker.ingest.gemini_json", side_effect=fake_gemini), \
             patch("worker.ingest.time.sleep", return_value=None):
            result = analyze_edition_for_users("edition-1", "2026-11-16", "EDIÇÃO EXTRA")

        self.assertEqual(result, {"users": 2, "processed": 2, "failed": 0})
        match_writes = [rows for table, rows in writes if table == "user_publication_matches"]
        self.assertEqual(len(match_writes), 2)
        self.assertEqual({rows[0]["user_id"] for rows in match_writes}, {"user-1", "user-2"})

    def test_regular_and_extra_publication_keys_are_separate(self):
        item = {"title": "Ato de exemplo", "text": "Texto da publicação"}
        regular = publication_key("2026-10-29", item, "regular")
        extra = publication_key("2026-10-29", item, "extra", "extra-a")
        second_extra = publication_key("2026-10-29", item, "extra", "extra-b")
        self.assertNotEqual(regular, extra)
        self.assertNotEqual(extra, second_extra)


if __name__ == "__main__":
    unittest.main()
