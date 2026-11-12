"""Persistent, bounded loot/time samples for the last 50 completed attacks."""
LIMIT = 50
FIELDS = ('battle', 'uptime_seconds', 'elapsed_seconds', 'gold', 'elixir', 'dark_elixir')


def valid_history(value):
    return (isinstance(value,list) and len(value)<=LIMIT and all(
        isinstance(row,dict) and all(type(row.get(key)) is int and row[key]>=0 for key in FIELDS)
        and row['battle']>0 and row['elapsed_seconds']>0 for row in value))


def merge_history(*histories):
    rows={}
    for history in histories:
        if valid_history(history):
            for row in history:
                old=rows.get(row['battle'])
                if old is None or row['uptime_seconds']>old['uptime_seconds']:
                    rows[row['battle']]=dict(row)
    return [rows[key] for key in sorted(rows)[-LIMIT:]]


def village_history(stats):
    cutoff = stats.get('rph_village_start_battle', 0)
    return [row for row in merge_history(stats.get('rph_history', [])) if row['battle'] > cutoff]


def append_battle(stats, gained, uptime):
    history = village_history(stats)
    previous=(history[-1]['uptime_seconds'] if history else
              stats.get('rph_window_start_uptime_seconds',stats.get('rph_updated_uptime_seconds',0)))
    elapsed=max(1,int(uptime)-int(previous))
    row=dict(battle=stats['battles_completed'],uptime_seconds=int(uptime),elapsed_seconds=elapsed,
             **{key:max(0,int(gained.get(key,0))) for key in ('gold','elixir','dark_elixir')})
    stats['rph_history']=merge_history(history,[row])


def rates(history):
    history=merge_history(history)
    seconds=sum(row['elapsed_seconds'] for row in history)
    return ({key:round(sum(row[key] for row in history)*3600/seconds) if seconds else None
             for key in ('gold','elixir','dark_elixir')},len(history))
