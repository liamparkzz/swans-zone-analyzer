# SWANS Zone Analyzer

MQSim/SWANS와 같은 용량·stripe·zone 계산을 사용해 **빈 zone 개수와 번호**를 구하는 독립 분석기입니다.
MQSim 실행이나 수정 없이 주소/길이만 분석하며 SSD I/O·FTL·GC·마모·migration을 모사하지 않습니다.

Python **3.10 이상**, 표준 라이브러리만 필요합니다. 대용량 원본 CSV에는 선택적으로 Windows C++ 보조 파서를 사용합니다.

## 현재 기준

- SSD 원시 용량 **32 GiB × 4**, NAND OP **10%**, SSD당 reserve **1 zone**.
- **zone = stripe = 32 MiB = 65,536 LBA**, LBA당 **512 byte**.
- user zone **3,680개**, reserve **4개**. reserve는 사용자 주소 범위와 최적화 대상에서 제외합니다.
- **주소 접기 금지:** `--no-folding`을 지정하며, 원본 전체가 장치 주소 범위 안에 들어가는 파일만 조합에 사용합니다.
- 기본 사용 여부는 **read+write 합집합**입니다. 기존 SWANS는 read 최초 접근도 데이터로 등록합니다.

[32 MiB 설정](configs/32gibx4-zone32mib/SETTINGS.md)과 비교용 [16 MiB 설정](configs/32gibx4-zone16mib/SETTINGS.md)을 함께 보관합니다.

## 트레이스의 빈 zone 확인

저장소 루트에서 실행합니다. 입력 경로와 새 출력 폴더를 지정하세요.

```shell
python zone_check.py --ssd configs/32gibx4-zone32mib/ssdconfig.xml --swans configs/32gibx4-zone32mib/swansconfig.xml --trace /path/to/io.trace --out results/case-A --no-folding
```

입력은 MQSim ASCII 5열 `time device_id LBA sector_count op`입니다. `0=write`, `1=read`이며 주소/길이는 512-byte sector 단위입니다.
CSV 원본을 직접 분석하려면 아래 원본 MSRC 분석기를 사용합니다.

- `--trace` 뒤에 여러 파일을 지정하면 **동일한 한 flow의 논리 주소 공간**에서 합집합을 구합니다. device_id로 SSD를 선택하지 않습니다.
- 선택 사항인 `--workload`는 단일 scenario/trace flow의 자원 할당과 실행 비율을 반영합니다. 없으면 한 flow가 모든 flash 자원을 사용합니다.
- `--access writes`는 쓰기 주소만 계산합니다.
- `--out`은 새 폴더여야 하며 기존 결과를 덮어쓰지 않습니다.

출력은 `summary.json`, zone별 `zones.csv`, 빈 user zone 번호만 담은 `empty_zones.txt`입니다.
`empty=True`가 빈 zone이고 ID는 0부터 시작합니다.

## 원본 MSRC 파일 조합 최적화

7열 원본 `Timestamp,Hostname,DiskNumber,Type,Offset,Size,ResponseTime` CSV를 직접 읽습니다.
Offset/Size는 byte 단위이며 입력 파일을 compact하거나 다시 쓰지 않습니다.

```shell
python find_trace_combination.py --ssd configs/32gibx4-zone32mib/ssdconfig.xml --swans configs/32gibx4-zone32mib/swansconfig.xml --root /path/to/MSRC1 /path/to/MSRC2 --out results/combination --no-folding
```

- 파일 이름에 `compact`가 있으면 제외합니다. `xxx_original.csv`가 있으면 `xxx.csv`보다 명시적 원본을 우선합니다.
- **원본 파일 전체를 선택하는 조합** 중 user zone 전체를 덮는 **read+write 요청수 합계**를 최소화합니다. prefix 자르기나 요청 골라내기를 하지 않습니다.
- `OPTIMAL`은 제공한 후보 집합 안에서 정확한 branch-and-bound로 최적성을 확인했다는 뜻입니다.
  전부 합쳐도 없는 zone이 있으면 `INFEASIBLE`과 해당 ID를 기록합니다.
- 접기 금지 결과는 `no-folding-results.json`, `no-folding-empty-zones.txt`로 저장합니다.
- 최초 스캔에서 zone별 비트를 만들고 이미 사용된 zone의 갱신은 건너뜁니다.
  이후 조합 비교는 파일별 bitmap으로 수행합니다. 중복 요청도 전체 파일의 비용에는 포함합니다.

### Compact 포함·범위 밖 요청만 제외·쓰기 도달 확인

```shell
python find_trace_combination.py --ssd configs/32gibx4-zone16mib/ssdconfig.xml --swans configs/32gibx4-zone16mib/swansconfig.xml --root /path/to/MSRC1 /path/to/MSRC2 --out results/compact-zone16mib --include-compact --no-folding --outside-range skip-request --access writes --objective-requests in-range
```

- `--include-compact`는 compact와 대체 처리본을 후보에 포함합니다.
- `--outside-range skip-request`는 사용자 범위 밖 요청만 제외합니다. 기본 `reject-file`은 해당 원본 파일 전체를 제외합니다.
  두 경우 모두 주소를 다른 곳으로 접지 않습니다.
- `--access writes`는 모든 user zone에 쓰기가 도달해야 하는 조건입니다. 기본은 read+write 합집합입니다.
- `--objective-requests in-range`는 범위 내에 남은 **read+write 전체 요청수**를 최소화합니다.
  기본 `source`는 필터링 전 원본 요청수를 비용으로 사용합니다.
- 쓰기 전용 분석에는 주소 접기 비활성으로 만든 쓰기 bitmap이 필요합니다.
  과거 주소 접기/접근 합집합 캐시를 쓰기 전용 bitmap으로 잘못 재사용하지 않습니다.

### 선택적 Windows 고속 파서

Visual Studio C++ Build Tools가 설치된 PowerShell에서:

```powershell
./scripts/build_native.ps1
```

그 뒤 조합 분석 명령에 `--native-parser ./msrc_scan_native.exe`를 추가합니다.
제어·최적화는 Python이며 보조 파서는 CSV 해석과 hash/footprint 계산만 수행합니다.
Python 구현과 원본 파싱·SHA-256이 같은지 테스트합니다.

### 기존 bitmap 재사용

작은 zone으로 만든 전체 스캔 결과가 있으면 `--no-folding --coarsen-cache /path/to/combination-results.json`으로 큰 zone을 재평가할 수 있습니다.
zone=stripe, 정수배 크기, 비증가 user-visible 용량이라는 조건을 검사합니다. source size/mtime이 바뀐 파일은 재사용하지 않습니다.
과거 주소 접기 비교 결과를 생성할 때에는 `--no-folding`을 생략할 수 있지만, **현재 연구 조건은 접기 금지**입니다.

## 계산과 해석 범위

용량은 plane별로 `int(blocks × pages × sectors_per_page × (1-OP))`를 계산한 뒤 합산합니다.
reserve 사용 시 SSD당 `floor(backend_sectors / zone_size)-1`개의 user zone과 1개의 reserve를 구분합니다.

```text
stripe_index = LBA // stripe_size
initial_ssd = stripe_index % ssd_count
local_LBA = (stripe_index // ssd_count) * stripe_size + LBA % stripe_size
zone_id = (local_LBA // zone_size) * ssd_count + initial_ssd
```

경계 요청은 닿는 zone을 모두 표시합니다. 덮어쓰기는 사용 zone 수를 늘리지 않습니다.
`initial_ssd`는 이동 전 배치이며 migration 후 실제 위치가 아닙니다.
초기 NAND 데이터, 시간별 I/O 완료, trace pool 무작위 선택, EOL 시점은 모사하지 않습니다.
preconditioning 설정이 있어도 트레이스에 없는 초기 주소는 추정하지 않습니다.
여러 NVMe flow의 주소 공간을 다시 분할하는 실험도 별도로 구분해야 합니다.

주소 접기 허용 비교 모드는 기준 코드의 `LBA % (capacity-1)`와 NVMe 주소 결합식 `(high32 << 31) | low32`를 따릅니다.
`address_footprint_optimum`은 실제 MQSim 재생 성공과 다를 수 있어 `native_replay_warnings`를 확인해야 합니다.
현재 원본 분석에는 **반드시 `--no-folding`**을 사용합니다.

## 호환 기준과 보관 결과

주소·용량 계산의 기준은 [liamparkzz/OS_RAID_PROJ](https://github.com/liamparkzz/OS_RAID_PROJ)의
커밋 [`5e9e2fe6d42831c23d6720bd3169735b5fbc628b`](https://github.com/liamparkzz/OS_RAID_PROJ/commit/5e9e2fe6d42831c23d6720bd3169735b5fbc628b)입니다.
`RAID_Device.cpp`, `Host_System.cpp`, `Logical_Address_Partitioning_Unit.cpp`, `IO_Flow_Trace_Based.cpp`,
`zone_directory.cpp` 및 host NVMe 요청 변환을 확인했습니다. 다른 fork의 규칙이 바뀌면 해당 차이를 확인해야 합니다.
원본 MQSim 관련 저작권 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 보존합니다.

[MSRC 결과 요약](examples/results/README.md)에 원본 후보 35개의 주소 접기 금지 결과를 보관했습니다.
**원본 트레이스·실행 파일·캐시는 배포하지 않습니다.** 요약에는 파일명·요청수·SHA-256을 남겼습니다.

## 검증

```shell
python -m unittest -v test_zone_check.py test_trace_combination.py
```

보조 실행 파일이 없으면 native 비교 테스트 하나만 건너뜁니다. 전체 검증은 보조 파서를 빌드한 후 실행합니다.
무작위 set-cover를 전수 subset 정답과 비교하고 zone 경계·덮어쓰기·read-only·주소 범위·캐시 병합을 확인합니다.

`verify_combination_result.py <result-directory>`는 과거 16 MiB 주소 접기 비교 결과의 보조 검증기입니다.
해당 결과·원본·native 파서가 있어야 하며 현재 접기 금지 결과와 구분합니다.
