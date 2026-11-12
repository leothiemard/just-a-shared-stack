import unittest

import jashelp  # noqa: F401  (puts the repo on sys.path)

from jas.sessions import scrub

R = "[redacted]"
REDACTED = [
    ("token='abcdef1234567890'", f"token='{R}'"),
    ("export DATABASE_PASSWORD='correct horse battery staple'", f"export DATABASE_PASSWORD='{R}'"),
    ('password="correct horse battery staple"', f'password="{R}"'),
    ('{"password": "hunter2"}', f'{{"password": "{R}"}}'),
    ('{"api_key": "abc123", "name": "x"}', f'{{"api_key": "{R}", "name": "x"}}'),
    ("password: hunter2", f"password: {R}"),
    ("secret: 'two words'", f"secret: '{R}'"),
    ('client_secret = "s3cr3t value"', f'client_secret = "{R}"'),
    ("GITHUB_TOKEN=abc123def", f"GITHUB_TOKEN={R}"),
    ("auth_token=abc123", f"auth_token={R}"),
    ("aws_secret_access_key=wJalrXUtnFEMI", f"aws_secret_access_key={R}"),
    ("access_key: AKIDEXAMPLE", f"access_key: {R}"),
    ("ApiKey=xyz", f"ApiKey={R}"),
    ("API_KEY=abc123 next", f"API_KEY={R} next"),
    ("db-password=pa55", f"db-password={R}"),
    ("--password=hunter2", f"--password={R}"),
    ("passwd=hunter2", f"passwd={R}"),
    ("auth: user123", f"auth: {R}"),
    ("credentials=abc", f"credentials={R}"),
    ("CREDENTIAL='a b c'", f"CREDENTIAL='{R}'"),
    ("https://example.invalid/cb?token=abc123&next=1", f"https://example.invalid/cb?token={R}&next=1"),
    ('"refresh_token": "a\\"quoted\\" value"', f'"refresh_token": "{R}"'),
    ("spring.datasource.password=hunter2", f"spring.datasource.password={R}"),
    ("'secret': 'x y'", f"'secret': '{R}'"),
    ("token=xyz password=hunter2 secret=s", f"token={R} password={R} secret={R}"),
    ("https://user:pa55word@example.invalid/repo", f"https://user:{R}@example.invalid/repo"),
    ("Authorization: Bearer abc.def-123456", f"Authorization: Bearer {R}"),
    ("key sk-ant-abcdefghijklmnopqrstuvwxyz used", f"key {R} used"),
]
KEPT = [
    "monkey=banana",
    "keyboard=us",
    "token_count=5",
    "max_tokens: 4096",
    "author: someone",
    "keyof T",
    "id=123e4567-e89b-12d3-a456-426614174000",
    "commit 3f6a1349d0c2b1e8a7f6e5d4c3b2a1f0e9d8c7b6",
    "path=/home/user/project/src/index.ts",
    "key=src/index.ts",
    "primary_key=id",
    "authority: the city",
    "secretary=alice",
    "tokenizer: porter",
    "We rotate the token weekly and keep passwords in a vault.",
    "passwords are hashed",
    "Token:\nThe next line is prose.",
    "const authors = ['a', 'b']",
]


class ScrubTests(unittest.TestCase):
    def test_credentials_are_redacted(self):
        for raw, clean in REDACTED:
            with self.subTest(raw):
                self.assertEqual(scrub.text(raw), clean)

    def test_ordinary_text_is_kept(self):
        for raw in KEPT:
            with self.subTest(raw):
                self.assertEqual(scrub.text(raw), raw)
