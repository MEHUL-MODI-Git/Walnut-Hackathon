"""Print `start duration` for one named beat in a recorder timeline, for shell use."""
import json, sys

timeline, beat = sys.argv[1], sys.argv[2]
marks = json.load(open(timeline))["marks"]
hit = next((m for m in marks if m["beat"] == beat), None)
print(f"{hit['at']:.2f} {hit['for']:.2f}" if hit else "")
