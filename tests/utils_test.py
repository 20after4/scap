#!/usr/bin/env python2

import os
import unittest
from scap import utils


class UtilsTest(unittest.TestCase):
    def test_check_php_opening_tag(self):
        dir = os.path.join(os.path.dirname(__file__), 'data')
        files = os.listdir(dir)
        for filename in files:
            path = os.path.join(dir, filename)
            if filename.startswith('good'):
                utils.check_php_opening_tag(path)
                # No exception thrown
                self.assertTrue(True)
            elif filename.startswith('bad'):
                self.assertRaises(
                    ValueError,
                    utils.check_php_opening_tag,
                    path
                )