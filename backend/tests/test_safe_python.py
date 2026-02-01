
import sys
import os
import unittest

# Add backend to sys.path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from utils.safe_python import get_safe_executor

class TestSafePython(unittest.TestCase):
    def setUp(self):
        self.executor = get_safe_executor()

    def test_valid_calculation(self):
        code = "result = 1 + 1"
        res = self.executor.execute(code)
        self.assertTrue(res['success'])
        self.assertEqual(res['result'], 2)

    def test_valid_string_manipulation(self):
        code = "result = ticker.upper().strip()"
        res = self.executor.execute(code, {"ticker": " aapl "})
        self.assertTrue(res['success'])
        self.assertEqual(res['result'], "AAPL")

    def test_blocked_import(self):
        code = "import os"
        res = self.executor.execute(code)
        self.assertFalse(res['success'])
        self.assertIn("Blocked pattern", res['error'])

    def test_blocked_exec(self):
        code = "exec('print(1)')"
        res = self.executor.execute(code)
        self.assertFalse(res['success'])
        self.assertIn("Blocked pattern", res['error'])

    def test_rce_attempt(self):
        # This is the RCE payload that attempted to use getattr
        code = """
# Construct cls string
cls_str = "__cla" + "ss__"
base_str = "__ba" + "se__"
# getattr(..., cls_str) returns int class.
# getattr(int_cls, base_str) returns object class.
obj_cls = getattr(getattr((1), cls_str), base_str)

# Construct subcls string
sub_str = "__sub" + "classes__"
subclasses_method = getattr(obj_cls, sub_str)
classes = subclasses_method()
"""
        res = self.executor.execute(code)

        # This should now FAIL because getattr is removed
        self.assertFalse(res['success'], "RCE attempt should fail")
        self.assertIn("name 'getattr' is not defined", res['error'])

        return res

if __name__ == '__main__':
    unittest.main()
