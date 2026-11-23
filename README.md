# 목일중 골든벨

목일중학교 수업용 O/X·4지선다 골든벨입니다.

- [게임 접속](https://rjegjin.github.io/mokil-goldenbell/)
- [예시 CSV](samples/mokil_goldenbell_sample.csv)
- [CSV 작성 안내](samples/mokil_goldenbell_csv_guide.txt)

## 원작 출처 및 감사

이 프로젝트는 **공학하는 기술쌤(kotaeyeol)**의 **OXplayground** 원본 코드와 화면 구성을 많이 참고하고, 이를 바탕으로 목일중학교 수업용으로 수정한 버전입니다. 원작을 만들어 공유해 주신 분께 감사드립니다.

- 원작 저장소: https://github.com/kotaeyeol/OXplayground
- 원작자 GitHub: https://github.com/kotaeyeol
- 원본 코드: https://github.com/kotaeyeol/OXplayground/blob/3fa6313781196d48adbcd42679ab6257ffc585b6/index.html
- 원작의 제작자 표시와 게임 화면 워터마크를 유지했습니다.
- 주요 수정: 학교·화면 이름, CSV 문제 업로드와 검증, 교체 전 미리보기, 문제 목록 백업, 예시·작성 안내, 별도 Firebase 연결.

## 이용·배포 조건 확인 상태

2026-11-23 확인 기준, 원작 저장소에는 `LICENSE`와 `README`가 없으며 원본 HTML에서도 명시적인 이용·배포 조건을 확인하지 못했습니다.

출처 표기는 원작자의 수정·재배포 허락을 받았음을 의미하지 않습니다. 공개 배포에 대한 원작자의 허락 또는 별도 라이선스 조건 확인이 필요합니다. 이 저장소에서 원작 코드에 새로운 라이선스를 임의로 부여하지 않습니다.

라이선스가 없는 저장소에 관한 GitHub 안내: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository

## 70명 전체 실시간 동기화 버전 (시험 운영)

- [70명 전체 동기화 소스와 서버 실행 방법](full-sync-70/README.md)
- [WebSocket 게임 서버](full-sync-70/server.py)
- [통합 테스트](full-sync-70/test_server.py)

**기존 [GitHub Pages 게임](https://rjegjin.github.io/mokil-goldenbell/)은 이전 정적 Firebase 버전으로 유지됩니다.** 새 버전은 `full-sync-70/` 디렉터리의 Python/aiohttp WebSocket 서버에서 HTML과 소켓을 함께 제공해야 실행됩니다. GitHub Pages에 HTML만 배포해서는 새 게임이 작동하지 않습니다. 행사 전 실제 학교 Wi-Fi와 휴대폰으로 검증하세요.
