# 32 GiB × 4 SSD, zone = stripe = 32 MiB

사용자 후속 요청으로 zone과 stripe를 함께 **16 MiB → 32 MiB**로 변경했다.
`Stripe_Unit_LBA=65536`, `SWANS_Zone_Size_LBA=65536`, sector512byte다.
SSD XML과 별도 SWANS XML 모두 같은 zone 크기를 명시한다.

원시 용량32 GiB×4, OP10%, SSD당 reserve1개 및 나머지 이전 실험 파라미터는 유지했다.
원래 MQSim 소스/설정과 16 MiB preset은 바꾸지 않았다.

- SSD당 user zone **920개**, 전체 **3,680개**, user ID **0..3679**.
- reserve는 SSD당 **1개**, 전체 **4개**, ID **3680..3683**. 최적화 대상에서 제외한다.
- 정수 zone 정렬 및 reserve 적용 후 user-visible 용량은 SSD당 **28.75 GiB**, 합계 **115 GiB**.
  16 MiB 설정의115.125 GiB에서 줄어드는 것은 전체 zone 정렬과 reserve 크기 변경 때문이다.
- 주소 접기 금지. 원본 전체가 user-visible 주소 범위 안에 들어가는 파일만 조합으로 사용한다.

MSRC1·2 원본 우선 후보35개 중 적합20개를 모두 합쳐 **3,189개 사용 / 491개 빈 user zone**이다.
따라서 이 조건에서는 빈 zone0개 조합이 없다.
결과는 `examples/results/README.md`에 있다.
전체 유효 설정은 `effective-settings.json`에 저장했다.
