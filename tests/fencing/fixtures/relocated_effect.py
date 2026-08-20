"""POSITIVE fixture: an effect that has moved OUT of tools/.

Its source is handed to find_sites with the path of a DECLARED relocated-effect
module. The commit is still a tool effect — it just does not live in tools/ any
more — so it must still own an inventory row.
"""


def _write_citekey_cache(session, ref, citekey):
    ref.citekey = citekey
    session.commit()
