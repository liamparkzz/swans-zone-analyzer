# 32 GiB × 4 SSD / 16 MiB zone 설정

2026-11-19 사용자 요청에 따라 독립 Python 분석기용으로 만든 설정이다.
MQSim 소스와 원래 실험 설정은 수정하지 않았다.

## 기준과 변경

기준은 앞선 대화에서 확인한 최신 SWANS 실험본
`https://github.com/liamparkzz/OS_RAID_PROJ/tree/5e9e2fe6d42831c23d6720bd3169735b5fbc628b/ssdconfig.xml` 및 `swansconfig.xml`이다.
기준 소스 브랜치는 `codex/tec-swans-eol-op-zone-20261031`이다.

SSD XML은 이전 실험본에서 복사한 뒤 사용자 요청으로 OP만 20% → 10%로 변경했다. 원시 32 GiB × 4와 stripe/zone 16 MiB는 유지했다.
별도 SWANS XML의 `SWANS_Zone_Size_LBA`는 **0(자동) → 32768(명시적 16 MiB)**로 바꿨다.
원래 자동값도 `stripe 32768 × multiplier 1`이어서 계산된 zone 크기는 같다.
SWANS XML 값이 SSD XML의 같은 설정을 덮어쓰며 아래 값은 그 우선순위를 반영한다.

## 용량과 주소 분할

- SSD 수: **4개**, 원시 용량 **각 32 GiB**, 합계 **128 GiB**.
- SSD마다 channel **2**, chip/channel **1**, die/chip **4**, plane/die **4**.
- plane마다 block **2048**, block마다 page **128**, page **4096 byte(4 KiB)**.
- 따라서 block **512 KiB**, SSD마다 **32 plane**이며
  `2 × 1 × 4 × 4 × 2048 × 128 × 4096 = 34,359,738,368 byte = 32 GiB`다.
- LBA 단위 **512 byte**, zone **16 MiB = 16,777,216 byte = 32,768 LBA**.
- stripe unit도 이전과 같은 **16 MiB = 32,768 LBA**. zone 크기와는 별개 설정이다.
- `Zone_Stripe_Multiplier=1`, `SWANS_Zone_Size_LBA=32768`을 명시한다.
- NAND OP **10%**, `op_zone_migration=true`로 SSD마다 full zone **1개**를 reserve로 분리한다.
- MQSim의 plane별 정수 절삭 후 backend 용량은 SSD마다 **60,397,952 sector**다.
  full zone은 SSD마다 **1,843개**이며, reserve 1개를 제외한 user zone은 **1,842개**다.
- user zone 총 **7,368개**, ID **0..7367**. reserve 총 **4개**, ID **7368..7371**.
  reserve는 빈 user zone 수에서 제외한다. 남는 부분 공간은 host에 노출하지 않는다.
- 실제 트레이스 주소에 노출되는 용량은 SSD당 **28.78125 GiB**, 합계 **115.125 GiB**다.
  **32 GiB × 4는 user-visible 용량이 아니라 원시 용량이다.**

## 이전 실험에서 유지한 설정

- host **NVMe**, queue depth **1024**, queue fetch **512**, seed **321**.
- preconditioning **false**, data cache capacity **0**, page-level mapping,
  ideal mapping table **true**, CMT **2,097,152 byte**, plane allocation **CWDP**.
- GC **GREEDY**, soft threshold **0.1**, hard threshold **0.005**,
  copyback/preemptible GC 및 SSD 내부 dynamic/static wear leveling **false**.
- flash **TLC**. read **25 μs**, program **200 μs**, erase **1.5 ms**.
  PE limit **25회**는 기존 가속 실험값 그대로 보존했다.
- bad block retirement **true**, EOL mode **OP_RATIO**, threshold **0.05**.
  TEC EOL threshold **100%**는 저장값이며 현재 종료 모드가 아니다.
- SWANS **true**, metric **BAD_BLOCK_COUNT**, trigger **DYNAMIC_WEAR**,
  popularity **HALF_DECAY**. effective default/placement/migration epoch 모두 **40초**.
  별도 SWANS XML이 SSD XML의 placement 80초·migration 120초를 40초로 덮어쓴다.
- precautionary threshold **5**, balance unit **1 MiB**, concurrent migrations **1**,
  working queue limit **64**, migration slice time/sectors **0**,
  pause **1 ms**, pause-on-read **false**.
- popularity period 저장값은 **40초**이나 HALF_DECAY는 기존 구현의 migration 완료 기반 정의다.
  legacy critical threshold 저장값 **32**도 그대로 보존했다.

**독립 분석기는 용량·stripe·zone·reserve 및 주소 변환만 적용한다.**
위 latency·queue·마모·GC·migration·EOL 값은 이전 설정과의 대응을 위해 보존했으며
SSD 동작을 실행하거나 분석 결과에 반영하는 값이 아니다.
트레이스에 없는 초기 데이터도 생성하지 않는다.
기본 `--access read-write`이며, 쓰기 주소만 원하면 `--access writes`를 사용한다.

전체 유효 설정값과 원본 hash는 `effective-settings.json`에 저장했다.

## 실행

트레이스 경로와 새 결과 폴더를 지정한다. 기본은 단일 flow가 모든 flash 자원을 사용하는 주소 공간이다.
이전 workload에서 자원을 일부만 할당했다면 단일 trace-flow XML을 `--workload`로 함께 전달한다.

```powershell
python zone_check.py `
  --ssd configs/32gibx4-zone16mib/ssdconfig.xml `
  --swans configs/32gibx4-zone16mib/swansconfig.xml `
  --trace /path/to/io.trace `
  --out results/32gibx4-zone16mib-case-A --no-folding
```

`empty_zones.txt`에 빈 user zone 번호, `summary.json`에 개수,
`zones.csv`에 zone별 사용 여부가 기록된다. 실제 트레이스 분석은 아직 실행하지 않았다.
