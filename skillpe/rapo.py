import ast
import random
import string
from pathlib import Path
from .io import read as load

def upstream_templates(upstream):
    """Extract only literal/f-string templates, without executing upstream code."""
    folder = Path(upstream) / 'examples/Stage1_RAPO'
    trees = {name: ast.parse((folder / name).read_text()) for name in
             ['word_augment.py', 'rewrite_via_instruction.py', 'refactoring.py']}
    merge = next(n for n in ast.walk(trees['word_augment.py']) if isinstance(n, ast.JoinedStr)
                 and isinstance(n.values[0], ast.Constant) and n.values[0].value.startswith('Suppose you are a Text Rewriter'))
    direct = next(n for n in ast.walk(trees['rewrite_via_instruction.py']) if isinstance(n, ast.JoinedStr)
                  and isinstance(n.values[0], ast.Constant) and n.values[0].value.startswith('Please limit your output'))
    refactor = next(n.value.value for n in ast.walk(trees['refactoring.py']) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'template' for t in n.targets))
    def render(node, values):
        return ''.join(str(v.value) if isinstance(v, ast.Constant) else str(values[v.value.id]) for v in node.values)
    return render, merge, direct, refactor

class Retriever:
    def __init__(self, assets):
        import numpy as np
        import networkx as nx
        from sentence_transformers import SentenceTransformer
        self.np = np
        self.encoder = SentenceTransformer(str(assets/'author/all-MiniLM-L6-v2'), device='cpu', local_files_only=True)
        graph = assets/'author/graph_data'
        self.maps = {name: load(graph / (name + '_to_idx.json')) for name in ['place','verb','scenario']}
        self.embeds = {name: np.asarray(load(graph/file), dtype=np.float32) for name, file in
                       [('place','place_embed.json'),('verb','verb_words_embed.json'),('scenario','scenario_words_embed.json')]}
        for name, values in self.embeds.items():
            self.embeds[name] = values / np.maximum(np.linalg.norm(values, axis=1, keepdims=True), 1e-12)
        self.places = {v:k for k,v in self.maps['place'].items()}
        self.graphs = {kind:nx.read_graphml(graph/file) for kind,file in
                       [('verb','graph_place_verb.graphml'),('scenario','graph_place_scene.graphml')]}

    def __call__(self, row, seed):
        np = self.np
        vector = self.encoder.encode(row['original_prompt'], normalize_embeddings=True)
        place_ids = np.argsort(-(self.embeds['place'] @ vector), kind='stable')[:3]
        rng = random.Random(seed)
        words = set()
        for idx in place_ids:
            place = self.places[int(idx)]; words.add(place)
            for kind, graph in self.graphs.items():
                neighbors = sorted(graph.neighbors(place)) if place in graph else []
                sampled = rng.sample(neighbors, 5) if len(neighbors) >= 5 else neighbors
                valid = [w for w in sampled if w in self.maps[kind]]
                valid.sort(key=lambda w: (-float(self.embeds[kind][self.maps[kind][w]] @ vector), w))
                words.update(valid[:5])
                if kind == 'scenario':
                    cross = [w for w in sampled if w not in self.maps[kind] and w in self.maps['place']]
                    if cross:
                        words.add(max(cross, key=lambda w:float(self.embeds['place'][self.maps['place'][w]] @ vector)))
        clean = sorted({w.translate(str.maketrans('', '', string.punctuation)) for w in words} - {''})
        vectors = self.encoder.encode(clean, normalize_embeddings=True)
        ranked = sorted([(w,round(float(v @ vector),4)) for w,v in zip(clean,vectors)], key=lambda x:(-x[1],x[0]))
        return dict(original_prompt=row['original_prompt'], retrieved_words=sorted(words), ranked_modifiers=ranked,
                    selected_modifiers=[w for w,score in ranked if score >= 0.6])
