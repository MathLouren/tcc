"""Testes isolados; nunca usam Qdrant de produção ou rede externa."""
import sys
import types
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.identidade import gerar_chunk_id, gerar_document_id, normalizar_url
from app.crawler import Coletor, descobrir_links, url_autorizada
from app.qdrant_incremental import preparar_pontos, upsert_incremental


def test_urls_canonicas_e_protecao_de_hosts():
    assert normalizar_url("https://universo.edu.br/a-universo/estrutura/sao-goncalo/?utm_source=x#bloco") == (
        "https://universo.edu.br/a-universo/estrutura/sao-goncalo/")
    assert url_autorizada("https://universo.edu.br/calendarios/mia/")
    assert not url_autorizada("https://universo.edu.br.evil.example/foo")
    assert not url_autorizada("http://localhost:8080/")
    assert not url_autorizada("file:///etc/passwd")
    assert not url_autorizada("https://universo.edu.br:8080/")


def test_ids_estaveis_e_sensiveis_a_mudancas():
    d = gerar_document_id("a.pdf", "https://universo.edu.br/calendarios/mia/")
    assert d == gerar_document_id("b.pdf", "https://universo.edu.br/calendarios/mia/?utm_campaign=x")
    assert gerar_chunk_id(d, "Seção", 0, "Prova V1 em novembro") == gerar_chunk_id(d, "Seção", 0, "Prova V1 em novembro")
    assert gerar_chunk_id(d, "Seção", 0, "Prova V1 em novembro") != gerar_chunk_id(d, "Seção", 0, "Prova V1 em dezembro")


def test_descobre_links_somente_relevantes_e_autorizados():
    html = b'<html><body><main><a href="/calendarios/mia/">MIA</a>' \
           b'<a href="https://malicioso.example/">Manual</a>' \
           b'<a href="/politica-de-privacidade/">Politica</a></main></body></html>'
    assert descobrir_links(html, "https://universo.edu.br/a-universo/estrutura/sao-goncalo/") == [
        "https://universo.edu.br/calendarios/mia/"
    ]


def test_crawler_respeita_robots_e_bloqueia_redirect_externo():
    def responder(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /privado/\n")
        if request.url.path == "/calendarios/mia/":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>OK</body></html>")
    cliente = httpx.Client(transport=httpx.MockTransport(responder))
    coletor = Coletor(cliente=cliente, pausa=0)
    with pytest.raises(ValueError, match="robots.txt"):
        coletor.baixar("https://universo.edu.br/privado/mia/")
    with pytest.raises(ValueError, match="Redirecionamento"):
        coletor.baixar("https://universo.edu.br/calendarios/mia/")
    cliente.close()


@pytest.fixture
def modelos_falsos(monkeypatch):
    class PointStruct:
        def __init__(self, id, vector, payload):
            self.id, self.vector, self.payload = id, vector, payload

    class PointIdsList:
        def __init__(self, points):
            self.points = points

    class MatchValue:
        def __init__(self, value):
            self.value = value

    class FieldCondition:
        def __init__(self, key, match):
            self.key, self.match = key, match

    class Filter:
        def __init__(self, must):
            self.must = must

    models = SimpleNamespace(PointStruct=PointStruct, PointIdsList=PointIdsList,
                             MatchValue=MatchValue, FieldCondition=FieldCondition, Filter=Filter)
    mod = types.ModuleType("qdrant_client")
    mod.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", mod)
    return models


class QdrantFalso:
    def __init__(self):
        self.pontos = {10: {"arquivo": "antigo.md", "texto": "Versão antiga", "_bloco_id": 0}}
        self.excluidos = []
        self.verificacao_ok = True

    def scroll(self, *, collection_name, scroll_filter, limit, with_payload, with_vectors, offset=None):
        nome = scroll_filter.must[0].match.value
        return [SimpleNamespace(id=i, payload=p) for i, p in self.pontos.items() if p["arquivo"] == nome], None

    def upsert(self, *, collection_name, points, wait):
        for p in points:
            self.pontos[p.id] = p.payload

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        if not self.verificacao_ok:
            return []
        return [SimpleNamespace(id=i, payload=self.pontos[i]) for i in ids if i in self.pontos]

    def delete(self, *, collection_name, points_selector, wait):
        self.excluidos.extend(points_selector.points)
        for i in points_selector.points:
            self.pontos.pop(i, None)


def test_upsert_preserva_outros_arquivos_e_e_idempotente(modelos_falsos):
    banco = QdrantFalso()
    banco.pontos[11] = {"arquivo": "outro.pdf", "texto": "Não tocar"}
    bloco = {"arquivo": "antigo.md", "documento": "Antigo", "secao": "Geral",
             "texto": "Versão nova", "url": "", "categoria": "teste"}
    pontos = preparar_pontos([bloco], np.ones((1, 3), dtype="float32"))
    primeiro = upsert_incremental(banco, "teste", pontos)
    assert primeiro["atualizados"] == 1
    # O ponto inteiro legado é preservado na primeira migração; somente a nova
    # versão estável é removível em execuções posteriores.
    assert 10 in banco.pontos and 11 in banco.pontos
    assert 10 not in banco.excluidos
    segundo = upsert_incremental(banco, "teste", pontos)
    assert segundo["inalterados"] == 1
    assert len(banco.pontos) == 3


def test_falha_de_verificacao_nao_exclui_legado(modelos_falsos):
    banco = QdrantFalso()
    banco.verificacao_ok = False
    blocos = [{"arquivo": "antigo.md", "documento": "Antigo", "secao": "Geral", "texto": "Alterado"}]
    pontos = preparar_pontos(blocos, np.ones((1, 3), dtype="float32"))
    with pytest.raises(RuntimeError, match="Falha de verificação"):
        upsert_incremental(banco, "teste", pontos)
    assert 10 in banco.pontos
    assert banco.excluidos == []


def test_qdrant_recupera_payload_sem_blocos_json():
    from app.indexacao import IndiceQdrant

    antigo = SimpleNamespace(id=12, score=0.81, payload={
        "id": 11, "_bloco_id": 11, "arquivo": "legado.md", "texto": "Critérios legados",
        "documento": "Critérios", "secao": "Avaliação"
    })
    novo = SimpleNamespace(id="c4b724e1-944f-4a72-ae5c-62e7bbbd9100", score=0.92, payload={
        "chunk_id": "c4b724e1-944f-4a72-ae5c-62e7bbbd9100", "arquivo": "crawler.pdf",
        "texto": "Calendário recente", "documento": "Calendário", "secao": "Novembro"
    })

    class Cliente:
        def query_points(self, **kwargs):
            assert kwargs["with_payload"]
            return SimpleNamespace(points=[novo, antigo])

        def scroll(self, **kwargs):
            return [novo, antigo], None

    indice = IndiceQdrant(Cliente(), "colecao_teste", [])
    resultados = indice.buscar(np.ones(3, dtype="float32"), 4)
    assert [r["texto"] for r in resultados] == ["Calendário recente", "Critérios legados"]
    assert resultados[0]["similaridade"] == 0.92
    assert {r["arquivo"] for r in indice.listar_blocos()} == {"crawler.pdf", "legado.md"}
