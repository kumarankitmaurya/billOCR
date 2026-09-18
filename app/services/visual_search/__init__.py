"""Visual product search: embed catalog photos, find designs by photo.

Deliberately not a generative-LLM feature. An image-embedding model turns
photos into vectors; pgvector finds the nearest ones. A VLM appears only in
attr_tagger.py, behind a flag, and never decides what matches.
"""
