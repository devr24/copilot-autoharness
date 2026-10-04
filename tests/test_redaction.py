import unittest

from harness.redaction import redact_text, redact_value


class RedactionTests(unittest.TestCase):
    def test_redacts_tokens_credentials_and_connection_strings(self):
        text = (
            "ghp_abcdefghijklmnopqrstuvwxyz123456 password=hunter2 "
            "Bearer abcdefghijklmnop AccountKey=secret-value "
            "https://user:secret@example.test/path GITHUB_TOKEN=github-secret "
            "AWS_SECRET_ACCESS_KEY=aws-secret"
        )
        result = redact_text(text)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("abcdefghijklmnop", result)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("user:secret@", result)
        self.assertNotIn("github-secret", result)
        self.assertNotIn("aws-secret", result)

    def test_redacts_private_key_block_and_bounds_strings(self):
        value = {"key": "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----"}
        result = redact_value(value, max_string=12)
        self.assertLessEqual(len(result["key"]), 40)
        self.assertNotIn("secret", result["key"])


if __name__ == "__main__":
    unittest.main()
