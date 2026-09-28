import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path

from mercury.__main__ import main,parser


class WorkflowCLITests(unittest.TestCase):
    def test_help_includes_data_only_bounded_workflow(self):
        help_text=parser().format_help()
        for command in ('observation','cycle','backtest','compare','configure-risk','workflow-demo'):
            self.assertIn(command,help_text)

    def call(self,arguments):
        output=io.StringIO();error=io.StringIO()
        with redirect_stdout(output),redirect_stderr(error):code=main(arguments)
        self.assertEqual(code,0,error.getvalue())
        return json.loads(output.getvalue())

    def test_saved_example_backtest_compare_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);configs=root/'configuration'
            created=self.call(['workflow-example','--out',str(configs)])
            one=self.call(['backtest','--episode',created['episode'],'--agent',created['agent'],'--out',str(root/'single')])
            self.assertEqual(one['models']['strategy']['report']['fills'],2)
            args=['compare','--episode',created['episode'],'--agents',created['agents'],'--out',str(root/'compare')]
            result=self.call(args);self.assertEqual(self.call(args),result)
            self.assertEqual(set(result['models']),{'offline-a','offline-b'})
            self.assertEqual(result['currency'],'CNY')
