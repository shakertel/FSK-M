"""CPU-only deployment check. No Telegram messages; Gemini called only with --query."""
import argparse
import json
from engine import FSKEngine

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--query',help='Optional real Gemini search; consumes API quota')
    args=parser.parse_args()
    engine=FSKEngine()
    print('DATABASE LOAD OK:',len(engine.movies),'movies; batch=400; logical_lanes=2; live_api=1; gap=3s')
    if args.query:
        result=engine.search(args.query)
        print(json.dumps(result[:3],ensure_ascii=False,indent=2))
