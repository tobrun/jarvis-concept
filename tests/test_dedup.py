from conftest import run_script
from dedup import key


def test_same_thread_same_text_with_extra_spaces_gives_the_same_key():
    original = key("slack:C01:1729600000.000100", "Can you review the Q4 plan?")
    spaced = key("slack:C01:1729600000.000100", "  Can you   review\nthe Q4\tplan?  ")
    assert spaced == original


def test_same_thread_edited_text_gives_a_different_key():
    original = key("slack:C01:1729600000.000100", "Can you review the Q4 plan?")
    edited = key("slack:C01:1729600000.000100", "Can you review the Q4 plan by Friday?")
    assert edited != original


def test_key_is_thread_id_and_sixteen_hex_digits_of_sha256():
    # sha256("abc") = ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
    assert key("gmail:t1", " abc ") == "gmail:t1:ba7816bf8f01cfea"


def test_key_is_seen_once_appended_to_the_surfaced_ledger(repo, tmp_path):
    text = tmp_path / "message.txt"
    text.write_text("abc")
    record = tmp_path / "surfaced.json"

    made = run_script(repo, "dedup.py", "key", "gmail:t1", "--file", str(text)).stdout.strip()
    before = run_script(repo, "dedup.py", "seen", made)
    record.write_text(f'{{"key": "{made}", "raw_id": "gmail:m1"}}')
    run_script(repo, "append.py", "surfaced", "--file", str(record))
    after = run_script(repo, "dedup.py", "seen", made)

    assert made == "gmail:t1:ba7816bf8f01cfea"
    assert before.returncode == 1
    assert after.returncode == 0
