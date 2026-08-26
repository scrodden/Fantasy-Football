"""Football game-outcome projection & betting-edge engine.

A self-updating model that projects a winner and score for every NFL and FBS
college football game each week, compares those projections against sportsbook
lines, and surfaces the spread / total / moneyline bets with positive expected
value.  Each week's results feed back into the ratings so next week's forecasts
are sharper.  Free data only, Python standard library only.
"""
