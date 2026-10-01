import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import json
import urllib.error

# Set up path to import scripts.check_cycle and localization properly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

import localization
import check_cycle as cc

class TestCheckCycle(unittest.TestCase):
    @patch('check_cycle.get_github_token')
    @patch('urllib.request.build_opener')
    def test_get_latest_github_release_with_token(self, mock_build_opener, mock_get_token):
        mock_get_token.return_value = "fake_token"
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"tag_name": "v1.0"}).encode()
        
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_response
        mock_build_opener.return_value = mock_opener
        
        tag, method, url = cc.get_latest_github_release("https://api.github.com/fake")
        self.assertEqual(tag, "v1.0")
        self.assertEqual(method, "api")
        self.assertEqual(url, "https://api.github.com/fake")
        
        req = mock_opener.open.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer fake_token")

    @patch('check_cycle.get_github_token')
    @patch('urllib.request.build_opener')
    def test_get_latest_github_release_no_token(self, mock_build_opener, mock_get_token):
        mock_get_token.return_value = None
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"tag_name": "v1.0"}).encode()
        
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_response
        mock_build_opener.return_value = mock_opener
        
        tag, method, url = cc.get_latest_github_release("https://api.github.com/fake")
        self.assertEqual(tag, "v1.0")
        self.assertEqual(method, "api")
        self.assertEqual(url, "https://api.github.com/fake")
        
        req = mock_opener.open.call_args[0][0]
        self.assertIsNone(req.headers.get("Authorization"))

    @patch('check_cycle.get_github_token')
    @patch('urllib.request.build_opener')
    @patch('check_cycle.get_fallback_github_release_tag')
    def test_get_latest_github_release_rate_limited(self, mock_fallback, mock_build_opener, mock_get_token):
        mock_get_token.return_value = None
        
        mock_opener = MagicMock()
        headers = {"X-RateLimit-Remaining": "0"}
        mock_opener.open.side_effect = urllib.error.HTTPError("url", 403, "Forbidden", headers, None)
        mock_build_opener.return_value = mock_opener
        
        mock_fallback.return_value = ("v1.1", "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v1.1")
        
        tag, method, url = cc.get_latest_github_release("https://api.github.com/fake")
        self.assertEqual(tag, "v1.1")
        self.assertEqual(method, "html")
        self.assertEqual(url, "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v1.1")
        mock_fallback.assert_called_once_with("https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest")

    @patch('urllib.request.urlopen')
    def test_get_fallback_github_release_tag_valid(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.geturl.return_value = "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v2.0"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        tag, url = cc.get_fallback_github_release_tag("https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest")
        self.assertEqual(tag, "v2.0")
        self.assertEqual(url, "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v2.0")

    @patch('urllib.request.urlopen')
    def test_get_fallback_github_release_tag_invalid_url(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.geturl.return_value = "http://example.com/not/a/tag"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        with self.assertRaises(ValueError):
            cc.get_fallback_github_release_tag("https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest")

    @patch('urllib.request.urlopen')
    def test_get_fallback_github_release_tag_invalid_path(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.geturl.return_value = "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/download/v2.0/test.zip"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        with self.assertRaises(ValueError):
            cc.get_fallback_github_release_tag("https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest")
            
    @patch('urllib.request.urlopen')
    def test_get_fallback_github_release_tag_invalid_auth(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.geturl.return_value = "https://user:pass@github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v2.0"
        mock_urlopen.return_value.__enter__.return_value = mock_response
        
        with self.assertRaises(ValueError):
            cc.get_fallback_github_release_tag("https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest")

if __name__ == '__main__':
    unittest.main()
