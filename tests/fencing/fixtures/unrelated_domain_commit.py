"""NEGATIVE fixture: an ordinary domain-layer commit.

Outside tools/ AND outside the declared relocated set. The domain layer commits
constantly and none of it is a tool effect; if this fixture ever starts producing
a site, the widening has become "scan everything" and the inventory becomes noise
nobody reads.
"""


def save_thing(session, thing):
    session.add(thing)
    session.commit()
