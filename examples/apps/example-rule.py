"""Example script rule. Save as config/apps/<app>.py and add to <app>.toml:

    [[rules]]
    id = "weekly"
    type = "script"
    once_per = "day"          # the server won't call this again for a subscriber
                              # today once it has returned a payload
    # file = "other.py"       # optional: a different file in the same folder
    # function = "should_send"

should_send(sub) runs about once a minute for every subscriber of the app
(skipping anyone this rule already sent to in the current once_per period).
Return a payload dict to send it, or None to send nothing. Edits are picked up
on the next tick; no restart needed. Exceptions are logged and skipped.

What `sub` offers:
    sub.id            subscriber id
    sub.prefs         dict the app sent when subscribing (e.g. {"time": "20:00"})
    sub.tz            their ZoneInfo time zone
    sub.local_now()   current datetime in their time zone
    sub.today()       "YYYY-MM-DD" in their time zone
    sub.has_mark(name, date=None)   did the app post this mark (default: today)?
    sub.created       when they subscribed (aware datetime)
"""


def should_send(sub):
    now = sub.local_now()
    # Sunday at 18:00 or later, unless they've marked the week as reviewed.
    if now.weekday() == 6 and now.hour >= 18 and not sub.has_mark("weekly-review"):
        return {"title": "Weekly review", "body": "Take a minute to look back on your week."}
    return None
