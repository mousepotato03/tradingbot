# Third-party deployment files

`seccomp_profile.json` is copied from Microsoft Playwright v1.63.0:

https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json

Playwright is licensed under Apache License 2.0. A copy of its license is included in `licenses/playwright-LICENSE.txt`. The profile permits the user namespace operations needed by the non-root Chromium sandbox. Dependencies installed by uv, apt and the container base images retain their own licenses; this notice does not relicense them.
