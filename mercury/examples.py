"""Synthetic A-share end-to-end fixtures, never labelled real market data."""
import json
import pathlib
import tempfile

from .common import digest
from .evaluation import compare_agents
from .models import OfflineAgent


def synthetic_episode():
    root = pathlib.Path(__file__).resolve().parent.parent
    calendar=json.loads((root/"config/cn-calendar-2026.json").read_text(encoding="utf-8"))
    ticker="600519.SH"
    instrument={"symbol":ticker,"profile":"SH_MAIN_STOCK","source":{"synthetic":True,"symbol":ticker,
                "reference":"synthetic workflow identity, not official security evidence","available_at":"2026-09-24T09:00:00+08:00"}}
    sessions=[{"symbol":ticker,"day":day,"reference_price":"10", "suspended":False,"risk_warning":False,
               "listing_session":100,"source":{"synthetic":True,"symbol":ticker,"day":day,
               "reference":"synthetic per-day status","available_at":day+"T09:00:00+08:00"}}
              for day in ("2026-09-24","2026-09-28")]
    prices=[("2026-09-24T09:58:00+08:00","9.99"),("2026-09-24T09:59:00+08:00","10"),("2026-09-24T10:00:00+08:00","10.01"),
            ("2026-09-24T10:01:00+08:00","10.01"),("2026-09-28T10:00:00+08:00","11"),
            ("2026-09-28T10:01:00+08:00","11")]
    sessions[1]["reference_price"]="10.01"
    events=[{"time":at,"decide":at.endswith("10:00:00+08:00")} for at,_ in prices[2:]]
    spec={"version":"cn-episode-v1","universe":[ticker],"cash":"100000","calendar":calendar,
          "instruments":[instrument],"sessions":sessions,"quotes":[{"ticker":ticker,"price":p,"asof":at,
          "evidence":{"synthetic":True,"frequency":"fixture","reference":"synthetic-workflow-example"}} for at,p in prices],
          "events":events,"documents":[],"lookback":60,"fee_bps":"3","slippage_bps":"0","minimum_commission":"5",
          "risk_policy":{"version":"cn-risk-v1","currency":"CNY","max_position_weight":"0.3",
          "max_order_notional":"30000","max_daily_turnover":"1","max_daily_loss":"0.2"},"synthetic":True}
    rows=[{"asof":at,"available_at":at,"value":p,"source_hash":digest({"synthetic":True,"time":at,"value":p})} for at,p in prices[2:]]
    spec["benchmark"]={"kind":"buy-hold-price-return","symbol":ticker,"currency":"CNY","universe":[ticker],
                       "reference":"synthetic aligned price-only benchmark","source_hash":digest(rows),"series":rows,
                       "includes_fees":False,"includes_dividends":False}
    return spec


def offline_template(weight="0.1"):
    return {"version":"cn-strategy-v1","signals":[{"name":"momentum","window":2,"weight":"1"}],
            "top_k":1,"allocation":{"gross_weight":weight,"weighting":"equal"},
            "rebalance":{"every_sessions":1,"anchor_day":"2026-09-24"},"holding":{"max_sessions":1},
            "invalidation":{"expires_at":"2026-09-28T07:00:00.000000+00:00","max_quote_age_seconds":300,"min_signal":"-1"},
            "reason":"Synthetic bounded momentum hypothesis; exit after one verified trading session"}


def workflow_demo():
    with tempfile.TemporaryDirectory(prefix="mercury-cn-workflow-") as directory:
        report=compare_agents(synthetic_episode(),{"offline-a":OfflineAgent(offline_template(),name="offline-a"),
                             "offline-b":OfflineAgent(offline_template("0.2"),name="offline-b")},output=directory)
        return {"synthetic":True,"model_api_called":False,"report":report}


def write_workflow_example(output):
    from .workflow import artifact
    root=pathlib.Path(output)
    configs={name:{"provider":"offline","name":name,"strategy":offline_template(weight)}
             for name,weight in (("offline-a","0.1"),("offline-b","0.2"))}
    artifact(root/"episode.json",synthetic_episode())
    artifact(root/"agent.json",configs["offline-a"])
    artifact(root/"agents.json",configs)
    return {"synthetic":True,"episode":str(root/"episode.json"),"agent":str(root/"agent.json"),
            "agents":str(root/"agents.json"),"model_api_called":False}
