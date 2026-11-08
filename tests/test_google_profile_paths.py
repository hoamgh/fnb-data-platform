from pathlib import Path

import pytest

from scripts.google_live_workers import validate_profile


def test_existing_dedicated_profile_in_another_worktree(tmp_path):
    profile=tmp_path/'other-worktree'/'output'/'google-crawler-login-02'
    (profile/'Default').mkdir(parents=True)
    (profile/'Local State').write_text('{}')
    (profile/'Default'/'Preferences').write_text('{}')
    assert validate_profile(profile,1)==profile.resolve()


@pytest.mark.parametrize('relative',['output/personal-profile','Chrome/User Data','other/google-crawler-login'])
def test_personal_or_non_dedicated_profile_is_rejected(tmp_path,relative):
    with pytest.raises(ValueError,match='Only dedicated'):
        validate_profile(tmp_path/relative,1)


def test_absent_profile_is_not_created(tmp_path):
    profile=tmp_path/'output'/'google-crawler-login'
    with pytest.raises(ValueError,match='missing'):
        validate_profile(profile,1)
    assert not profile.exists()
