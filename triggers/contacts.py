"""推卡之前找联系方式：给这一批要推的公司查公司概况、查帖子作者、抓官网邮箱，结果写进库。

    load_contacts_config   读 config/contacts.yaml
    plan_lookups           这一批里哪几家要抓公司详情（C1）、哪几个人要花钱查（P3 / P4）。纯函数
    quote_usd              照计划报价（按价格表的上限）。纯函数
    find_contacts_for_run  两条路都调这一个：这一跑要推卡的那几家，交给 find_contacts
    find_contacts          整个流程，见最后那一段「六步」
    card_contacts          拼卡片时读：每家的邮箱、官网、公司概况，和每个作者跟这家是什么关系

scheduled 和 interactive 两条路共用这一份，跟 digest.py 平级：

    scheduled      triggers/scheduled_discovery/main.py，那一跑提交之后、push_digest 之前调
    interactive    图里 run_harvest -> find_contacts -> deliver，那个节点在 nodes.find_contacts

判断作者属于哪一种情况的规则函数在 discovery/authorship.py（纯函数）；每种情况怎么处理、
落什么库，全写在这里。

── 表和表怎么连 ─────────────────────────────────────────────────────────────

    leads ─── lead_id ──┬── lead_evidence ── signal_id ── signals        原料，只读
      │                 ├── company_profiles                              C1 写
      │                 ├── person_lookups ─┐                             P3 / P4 / P9 写
      │                 └── lead_contacts ──┴── person_lookup_id          C1 / P3 / P4 写
      │
      └── id ── actor_spend.lead_id                                       每次花钱写

    月度上限另外要读采集的账：query_executions ── run_id ── runs

── 十一种情况：条件、处理、落什么库、为什么 ──────────────────────────────────

公司那一段看这家有没有 LinkedIn 公司 id，两种（C1、C2）。作者那一段看每条证据里的每个作者，
九种（P1 到 P9），转发的帖子有两个作者、各判一次。

C1  手上有 LinkedIn 公司 id（leads.linkedin_company_id 有值）
    处理    一、抓 LinkedIn 公司详情，花钱（$0.004 一家，一批一次 run 另收一次启动费）。
                LinkedIn 公司页只给公司概况和官网地址，不给邮箱。抓过的不再抓
            二、打开官网首页找邮箱，不花钱。官网地址就是第一步抓回来的那个；
                以前已经从官网找到过邮箱的不再抓
    落库    actor_spend       一行：花了多少
                              为什么：月度上限要把这笔算进已花
            company_profiles  一行：官网地址、人数、人数档、成立年份、总部、行业、actor 的原始返回
                              为什么：概况会变（人数会涨），每抓一次留一行才不丢旧的；原始返回留着，
                              以后要加卡片上的东西不用再花钱重抓。不塞进 leads：leads 记的是我们怎么判
                              这家，概况是这家自己长什么样
            lead_contacts     官网首页上每个域名跟官网一样的邮箱一行，source='website'、
                              person_lookup_id 空、四样可信度空
                              为什么：卡片要印，而且要知道出处；不是从某个人那里来的，所以不指回查人那一行；
                              公开写在官网上、没人验证过，所以可信度空着而不是填「很可信」
            leads.domain      官网跳转之后落到的那个地址的域名，如 'hushly.example'（网址，不是邮箱）
                              为什么：domain 是认「是不是同一家公司」的身份键之一（有唯一索引），
                              以后这家从别的来源进来能认出来。只在空着时写：已有的值可能来自更可靠的来源
            官网落在共用平台上的（linktr.ee、instagram.com 这种，名单在 config/contacts.yaml 的
            shared_hosts）：不找邮箱、不写 leads.domain，记进报告 shared_hosts。那个域名是平台的，
            写进 leads.domain 的话，第二家也填 linktr.ee 的公司会被误报成「两行是同一家公司」
    卡片    「公司概况」那一区，和官网上的邮箱
    例      Hushly（lead 1388）：概况 11 人（2-10）、2022 年成立、United States、Music；
            官网 http://www.hushly.example 跳到 https://home.hushly.example，找到 contact@hushly.example，
            leads.domain = 'hushly.example'
            ORBO Audio（lead 1287）：LinkedIn 登记的是 orbobrand.example，跳到 orboaudio.example，找到
            mark@orboaudio.example。拿跳转之前的 orbobrand.example 去比会把它当成别家的扔掉

C2  只有公司名（leads.linkedin_company_id 为空）
    处理    不抓公司详情、不抓官网：按名字去搜，搜到同名的另一家公司的风险太大
    落库    公司那一段什么都不写
            为什么：没有可靠的身份就不花钱，宁可空着，也不把别家的概况挂到这家头上
    卡片    没有「公司概况」那一区
    例      Chatterly Inc.（lead 1281）

作者那一段怎么判：每个作者按下面的顺序一关一关过，停在哪一关就是哪一种。
「规范化」是小写、标点变空格（& 和 - 留着）、去掉 inc / llc 这种后缀，公司名和职位串走同一套。

    第一关  作者是不是公司主页：signals.actor_type='company'
            （原帖作者库里没有这一列，看 reposted_actor_url 里有没有 /company/）
                是 -> 主页链接里 /company/ 后面那一段，等不等于 leads.linkedin_slug
                      （这家没有 slug，就拿主页名字规范化之后跟 leads.normalized_name 比）
                      等于 -> P1        不等于 -> P2
    第二关  作者是个人。这家的名字（leads.normalized_name）是不是只有一个词、而且太短或者在常用词表里
                是 -> P8
    第三关  这家的名字作为整词，出现没出现在这个人规范化之后的职位串（signals.actor_headline）里
                出现了 -> P3（通道 headline）或 P4（通道 mentioned），两种一样处理：花钱查他
    第四关  这家的名字有好几个词，其中某个词（三个字母以上、不在常用词表里）出现在职位串里
                是 -> P7
    都不是  -> P5

转发的帖子（P6）有两个作者，各从第一关过一遍。
P3 / P4 花钱查回来之后还有一关「对不对得上」，对不上的是 P9（见 P3）。

P1  作者是这家公司自己的公司主页
    判断    第一关：是公司主页，而且链接里的 slug 等于这家的 linkedin_slug
    落库    库里不变：不花钱，不写任何表
            为什么：「这是这家自己的主页」拿 signals 当场就算得出来；存下来就有两份答案，规则一改
            库里那份就是旧的。卡片要的主页链接和帖子链接 signals 里本来就有
    卡片    「这家公司自己的主页发的帖」，给公司主页链接和帖子链接
    例      Makers&Co（lead 1334）<- signal 2784
                第一关  actor_type='company'，是公司主页；
                        链接 'https://www.linkedin.com/company/makers&co/posts' 取出 'makers&co'，
                        == leads.linkedin_slug 'makers&co' -> P1
                库里    不变

P2  作者是别的机构的公司主页（媒体、投资方、展会）
    判断    第一关：是公司主页，但链接里的 slug 不等于这家的 linkedin_slug
    落库    库里不变，为什么同 P1
    卡片    「发帖的是 audioXpress（别的机构），不是这家公司的，但仍可联络」，给那个主页和帖子链接
    例      Earmark（lead 1341）<- signal 2789
                第一关  actor_type='company'，是公司主页；
                        链接 'https://www.linkedin.com/company/audioxpress/posts' 取出 'audioxpress'，
                        != leads.linkedin_slug 'earmarkaudio' -> P2
                库里    不变

P3  作者是个人，职位串里有这家的名字，通道是 headline
    判断    过了第一、二关，停在第三关：这家的名字作为整词出现在职位串里
    落库    花钱查他的主页并找邮箱（第 ⑤ 步），查回来之后再判三件事：

            一、查回来了没有
                没有（主页删了、链接不对）-> 只写 actor_spend；person_lookups 不写，下次还会再查他
            二、对不对得上：他现在的工作是不是就在这家
                比的是两样东西：
                    他那边  查回来的 currentPosition，也就是他经历里标着「至今」的每一份工作
                    这家那边  leads 那一行
                按下面的顺序比，两边都有的那一样才比，比了就以它为准、不往下退：
                    他现职的公司 id（companyId）        vs  leads.linkedin_company_id
                    他现职的公司页 slug                 vs  leads.linkedin_slug
                    他现职的公司名（规范化之后）        vs  leads.normalized_name
                有一份现职相等 -> 对得上；一份都不相等 -> 对不上，是 P9
            三、找没找到邮箱

            三件事判完：
                actor_spend      一行（同一次 run 查的几个人合一行）
                person_lookups   一行：lead_id、signal_id、他的主页链接、他的现职、matches_lead
                                 （对得上是 true）
                lead_contacts    对得上、而且找到了邮箱 -> 每个邮箱一行，source='linkedin_email_search'，
                                 person_lookup_id 指回上面那一行，四样可信度原样；
                                 没找到邮箱 -> 不写
            为什么：现职是花钱才拿得到的，存进 person_lookups，下次就不再花钱查他；matches_lead 是拿当时
            的 leads 比出来的，存下来以后才答得出「当时为什么挂了（或没挂）这个邮箱」。邮箱进 lead_contacts
            是为了印在卡片上，指回查人那一行，卡片才印得出「这是谁的邮箱」
    卡片    「Ravi Shah · 这家公司的人（Founder/CEO）」，有邮箱就印邮箱和可信度
    例      Chatterly Inc.（lead 1281）<- signal 2714 Ravi Shah，通道 headline
                第一关  actor_type='profile'，是个人
                第二关  'chatterly' 一个词，但不短、不在常用词表里，照常比
                第三关  职位串 'Founder/CEO Chatterly Inc. | Safe-listening headphones | Offset Audioᵀᴹ'
                        规范化成 'founder ceo chatterly safe-listening headphones offset audioᵀᴹ'，
                        里面有整词 'chatterly' -> P3，查他
                查回来  一、返回了
                        二、他的现职公司名 'Chatterly Inc.'，没有公司 id、没有 slug（这家公司在 LinkedIn 上
                            没有公司页）；这家 leads 也没有 id 和 slug。只能比名字：
                            'Chatterly Inc.' 规范化成 'chatterly' == leads.normalized_name 'chatterly' -> 对得上
                        三、emails 是 None，没找到邮箱
                库里    person_lookups  +1 行  lead_id=1281  signal_id=2714
                                               person_linkedin_url='https://www.linkedin.com/in/rshah'
                                               current_company_name='Chatterly Inc.'
                                               current_company_linkedin_url=NULL  matches_lead=true
                        lead_contacts   不变

P4  作者是个人，职位串里有这家的名字，通道是 mentioned（他在帖子里 @ 了这家）
    判断    跟 P3 一样停在第三关。判断只看职位串，不看通道：只看通道的话，mentioned 会被当成
            「提到这家的外人」，这家的创始人就漏了
    落库    跟 P3 一样
    卡片    跟 P3 一样
    例      Hushly（lead 1388）<- signal 2820 sam hadid，通道 mentioned
                第一关  actor_type='profile'，是个人
                第二关  'hushly' 不短、不在常用词表里，照常比
                第三关  职位串 'ceo @ Hushly - a sanctuary for human sound'
                        规范化成 'ceo hushly - a sanctuary for human sound'，里面有整词 'hushly' -> P4，查他
                查回来  一、返回了
                        二、他的现职公司 id '100000002'，这家 leads.linkedin_company_id 也是 '100000002'，
                            两边都有 id，比 id：相等 -> 对得上
                        三、找到 sam@hushly.example
                库里    person_lookups  +1 行  lead_id=1388  signal_id=2820
                                               person_linkedin_url='https://www.linkedin.com/in/sam-hadid-a1b2c3d4'
                                               current_company_name='Hushly'
                                               current_company_linkedin_url='https://www.linkedin.com/company/hushly/'
                                               matches_lead=true
                        lead_contacts   +1 行  lead_id=1388  value='sam@hushly.example'
                                               source='linkedin_email_search'
                                               person_lookup_id=<上面那一行的 id>
                                               email_status='risky'  catch_all=true  found_in_google=true
                                               quality_score=75

P5  作者是个人，职位串里没有这家的名字
    判断    过了第一、二、三、四关都没停下
    落库    库里不变，为什么同 P1
    卡片    「发帖的人不是这家公司的，只是在帖子里提到了它，仍可联络」，给他的主页和帖子链接
    例      Boomcrate（lead 1285）<- signal 2718 Arthur Mann
                第一关  是个人
                第二关  'boomcrate' 不短、不在常用词表里，照常比
                第三关  职位串规范化成 'cro bvf helping brands launch & scale crowdfunding & ecommerce
                        expert advisor kickstarter indiegogo'，里面没有 'boomcrate'
                第四关  'boomcrate' 只有一个词，不看「对上一部分」
                -> P5
                库里    不变

P6  转发的帖子：转发的人和原帖作者各判一次
    判断    signals.reposted_actor_url 有值，这条证据就有两个作者：转发的人（role='poster'，
            signals.actor_*）和原帖作者（role='original_poster'，signals.reposted_actor_*），各从第一关过一遍
    落库    自己不单独落，两个作者各按自己停下的那一种落。原帖作者要是停在第三关、花钱查了，
            person_lookups 那一行的 signal_id 记的是这条转发（原帖在库里没有单独一行）
    例      Boomcrate（lead 1285）<- signal 2718，Rob Olsen 发了关于 Boomcrate 的帖子，Arthur 转发了
                转发的 Arthur Mann    -> P5（过程见上面 P5 的例子），库里不变
                原帖作者 Rob Olsen
                    第一关  reposted_actor_url 是 'https://www.linkedin.com/in/rob-olsen-...'，里面没有
                            /company/，是个人
                    第二关  'boomcrate' 照常比
                    第三关  职位串 'CEO & Founder, Managing Partner & Celebrity Relations at Boomcrate'
                            规范化成 'ceo & founder managing partner & celebrity relations at boomcrate'，
                            里面有整词 'boomcrate' -> 停在第三关，跟 P3 一样查他
                    查回来之后照 P3 那三件事判，库里怎么变也照 P3；person_lookups 的 signal_id=2718

P7  作者是个人，职位串里只对上这家名字的一部分
    判断    停在第四关
    落库    库里不变：保守地当成不是这家的人，宁可漏掉，也不为猜的花钱
    卡片    「看不出发帖人是不是这家公司的，仍可联络」，给他的主页和帖子链接
    例      BVF Georgia（lead 1286）<- signal 2718 Arthur Mann
                第一关  是个人
                第二关  'bvf georgia' 两个词，不看这一关
                第三关  职位串规范化成 'cro bvf helping brands ...'，里面没有整个 'bvf georgia'
                第四关  'bvf'（三个字母、不在常用词表里）在职位串的词里，'georgia' 不在 -> P7
                库里    不变

P8  这家的名字只有一个词，而且太短或者是常用英文单词
    判断    停在第二关（多短算太短、常用词表在 config/contacts.yaml 的 name_match）
    落库    库里不变，跟 P5 一样处理。不比的原因：职位串里的 'air quality'、'on air'
            会被当成 Air 这家公司的人，白花钱
    卡片    跟 P5 一样
    例      Air（lead 1354）<- signal 2801 Luz Cortez
                第一关  是个人
                第二关  'air' 一个词、在常用词表里 -> P8
                库里    不变

P9  P3 / P4 查了，但他现在的工作不在这家（换工作了，职位串没更新）
    判断    停在第三关、花钱查了，查回来「二、对不对得上」那一关：他的每一份现职都跟这家不相等
    落库    actor_spend      一行，同 P3
            person_lookups   一行，matches_lead=false，现职记他现在那家
            lead_contacts    不写。他的邮箱只留在 person_lookups 那一行的 raw 里
            为什么：钱花了要留下查过的记录，下次才不再花钱查他，也答得出「为什么没挂邮箱」；
            他的邮箱多半是新公司的，挂在这家卡片上会发错人
    卡片    「他名字下面写着这家公司，但现在已不在这家」，给他的主页和帖子链接
    例      库里还没有真例子。假设的样子：替 Hushly（lead 1388）查一个人
                第三关  职位串 'Founder @ Hushly'，有整词 'hushly'，查他
                查回来  二、他唯一的现职是 Spotify，公司 id '207470'；Hushly 的 leads.linkedin_company_id
                            是 '100000002'，两边都有 id，比 id：不相等 -> 对不上 -> P9
                库里    person_lookups  +1 行  lead_id=1388  current_company_name='Spotify'
                                               matches_lead=false
                        lead_contacts   不变

── 以前已经有的东西：只往里加，不替换 ────────────────────────────────────────

同一家公司可能被推好几次（冷却期过了、又被搜到），每次推之前都会跑一遍这里。遇到库里已经有的行：

    lead_contacts      找到几个记几个。同一家、同一个来源、同一个地址（不分大小写）只存一次。
                       新找到一个以前没有的地址：新旧都留，卡片上新的在前。
                       这家以前已经有邮箱、这次又找到一个以前没有的地址 -> 报告 changed_emails 记一笔、
                       打一行日志，等人看一眼哪个对。第一次找到不算，同一个地址换了来源也不算
    leads.domain       只在空着时写。这个域名已经写在另一家那一行上 -> 不写，报告 domain_conflicts
                       记一笔、打一行日志，等人判断两行是不是同一家公司
    company_profiles   抓一次加一行；抓过的公司不再抓
    person_lookups     同一家、同一个人只有一行；查过的人不再查
    actor_spend        每调一次付费 actor 加一行，从来不改

── find_contacts 的六步 ─────────────────────────────────────────────────────

例子是八家库里的真公司，正好每种情况一家，以前一家都没找过：

    lead 1334 Makers&Co         C1   P1
    lead 1341 Earmark               C1   P2
    lead 1281 Chatterly Inc.          C2   P3
    lead 1388 Hushly                C1   P4
    lead 1285 Boomcrate              C1   P5，P6
    lead 1289 North Field Technologies C1   P6
    lead 1286 BVF Georgia           C1   P7
    lead 1354 Air                   C1   P8

① 读原料                                                                    只读
    leads              每家的身份：linkedin_company_id（分 C1 / C2）、linkedin_slug（P1 / P2 拿它比）、
                       normalized_name（P3 到 P8 拿它跟职位串比）
    lead_evidence      每家有哪几条证据（lead_id -> signal_id）。lead_type（通道）不读：
                       P3 和 P4 只差在通道，处理一样
    signals            每条证据的作者：actor_*；reposted_actor_* 有值的是转发的帖子（P6），多一个原帖作者
    company_profiles   抓过公司详情没有（C1 抓过的不再抓）
    person_lookups     查过哪些人（查过的不再查，而且占名额）
    lead_contacts      从官网找到过邮箱没有（找到过的 ⑥ 不再抓）；以前有哪些邮箱（changed_emails 拿它比）
    这一批八家都还没找过，后三张表查回来都是空的

② 定计划                                                                    不读不写
    公司：七家有公司 id、没抓过 -> 抓公司详情（C1）；Chatterly Inc. 只有名字 -> 不抓（C2）
    作者（每个作者判一次）：
        P1  Makers&Co  <- signal 2784 Makers&Co 自己的主页                              不查
        P2  Earmark        <- signal 2789 audioXpress 的主页                                    不查
        P3  Chatterly Inc.   <- signal 2714 Ravi Shah，'Founder/CEO Chatterly Inc. | ...'         查
        P4  Hushly         <- signal 2820 sam hadid，'ceo @ Hushly - a sanctuary ...'           查
        P5  Boomcrate       <- signal 2718 / 2748 Arthur Mann，'CRO @ BVF | ...'             不查
                           <- signal 2830 Ana Petrosyan，'8+ Years in Digital Marketing ...'  不查
        P6  Boomcrate       <- signal 2718 是 Arthur 转发的 Rob Olsen 的帖子：
                                也就是说，Rob 发了关于 Boomcrate 的帖子，该贴被 Arthur 转发了
                                转发人 Arthur Mann，职位串里没有 Boomcrate              P5，不查
                                原帖作者 Rob Olsen，'CEO & Founder ... at Boomcrate'         P3 那种，查
            Boomcrate       <- signal 2830 是 Ani 转发的 BVF Team 的帖子：
                                也就是说，BVF Team 发了关于 Boomcrate 的帖子，该贴被 Ani 转发了
                                原帖作者 BVF Team 的主页                                    P2，不查
            North Field       <- signal 2720 是 Marc-Andre 转发的 SingAlong Group 的帖子：
                                也就是说，SingAlong 发了关于 North Field 的帖子，该贴被 Marc-Andre 转发了
                                转发人 Marc-Andre Lavoie，'Founder & CEO, North Field ...' P3 那种，查
                                原帖作者 SingAlong Group 的主页                               P2，不查
        P7  BVF Georgia    <- signal 2718 Arthur Mann，职位串里有 'bvf'、没有 'bvf georgia'  不查
            （同一条 signal 2718 的原帖作者 Rob Olsen，替 BVF Georgia 判是 P5：他的职位串里是
             Boomcrate 不是 BVF Georgia。同一个人，替不同的公司判，结果可以不一样）
        P8  Air            <- signal 2801 Luz Cortez。'air' 在常用词表里，不比                    不查
    结果：抓公司详情七家；查四个人（Ravi Shah、sam hadid、Rob Olsen、Marc-Andre Lavoie）

③ 过闸                                                                      只读
    query_executions + runs   本账期对话采集计费了多少条
    actor_spend               本账期找联系方式花了多少
    报价 7 x 0.004 + 0.00005 + 4 x 0.01 = 0.06805，加上已花没超上限 -> 放行

④ 公司详情（C1）                                                            写 actor_spend、company_profiles
    一次 run 查那七家：先写 actor_spend 提交，再每家写一行 company_profiles 提交

⑤ 查人（P3 / P4，包括 P6 里落进 P3 那种的两个人）
                                                  写 actor_spend、person_lookups、lead_contacts
    一次 run 查那四个人：先写 actor_spend 提交，再逐人核对现职：
        P3  Ravi Shah 替 Chatterly Inc.   对上；没找到邮箱，只写 person_lookups
        P4  sam hadid 替 Hushly           对上；写 person_lookups，sam@hushly.example 写 lead_contacts
        P6  Rob Olsen 替 Boomcrate、Marc-Andre Lavoie 替 North Field Technologies
                                          对上就跟上面一样；对不上就是 P9
        P9  谁查回来现职不在这家          写 person_lookups（matches_lead=false），邮箱不写

⑥ 官网首页（C1，不花钱）                                                    写 lead_contacts、leads.domain
    ④ 抓回来有官网地址的那几家：打开首页，域名跟官网一样的邮箱写 lead_contacts；
    跳转之后落到的地址的域名写 leads.domain（写之前先查这家是不是空着、这个域名有没有被别家占着）。
    例：Hushly -> contact@hushly.example，leads.domain = 'hushly.example'

── 三条特别的规矩 ───────────────────────────────────────────────────────────

自己提交，跟 digest.py 那几个「只写不提交」不一样：每调一次付费 actor，钱就已经扣了，那一笔必须
马上落库。等到最后一起提交的话，中间哪一步崩溃整个回滚，钱花了库里却没有记录，月度上限就少算了
一笔。所以传进来的连接上不能有没提交的写。

超了月度上限不抛异常：跳过付费的两步（④⑤），免费的官网照抓（只抓以前已经有概况的），卡片照推。
报告里写明为什么没找。

调 actor 出错（Apify 挂了、run 失败、超时）也不抛异常：出错的那一步跳过，记进报告 actor_errors，
后面几步照做，卡片照推。找联系方式是锦上添花，不能因为它推不出卡片。
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import yaml

from adapters import harvestapi_company, harvestapi_profile, website
from delivery.cards import load_card_config
from discovery.authorship import classify_author, job_matches_lead, pick_current_job
from models import (
    NO_CONTACTS,
    AuthorKind,
    AuthorRef,
    CardContacts,
    CardEmail,
    CardPerson,
    CardRelation,
    CompanyProfile,
    ContactChannel,
    ContactSource,
    LeadForContacts,
    PersonLookup,
    SavedContact,
    SavedPersonLookup,
    SpendPurpose,
    linkedin_company_slug,
)
from persistence import contacts_db
from triggers.interactive_discovery.budget import MonthlyCapReached, check_monthly_cap, load_budget_config
from triggers.interactive_discovery.query.planner import load_planner_config

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "contacts.yaml"

# 记账时 actor 的名字写成斜杠，跟 Apify 后台和账单上的写法一样（API 路径里的是波浪号）
_COMPANY_ACTOR = harvestapi_company.ACTOR_ID.replace("~", "/")
_PROFILE_ACTOR = harvestapi_profile.ACTOR_ID.replace("~", "/")


@dataclass(frozen=True)
class ContactsConfig:
    """config/contacts.yaml 读出来的样子。

    一个实际值（仓库里那份配置）：

        ContactsConfig(company_usd=Decimal("0.004"), company_run_start_usd=Decimal("0.00005"),
                       person_with_email_usd=Decimal("0.01"), max_people_per_lead=2,
                       max_short_length=3, common_words=frozenset({"air", "fever", "nothing"}),
                       homepage_workers=8, shared_hosts=frozenset({"linktr.ee", "instagram.com"}))
                       （两个词表都只列了几个，全表在配置里）
    """

    company_usd: Decimal
    company_run_start_usd: Decimal
    person_with_email_usd: Decimal
    max_people_per_lead: int
    max_short_length: int
    common_words: frozenset[str]
    homepage_workers: int
    shared_hosts: frozenset[str]


def load_contacts_config(path: pathlib.Path | str = DEFAULT_CONFIG_PATH) -> ContactsConfig:
    """读 config/contacts.yaml。

    输入：配置文件路径，默认是仓库里那份
    输出：ContactsConfig，例子见那个类

    缺键当场 KeyError，不给默认值，跟仓库里别的 load_*_config 同一条口径。
    单价转成 Decimal 时先转字符串：Decimal(0.004) 会变成 0.004000000000000000083...
    """
    raw = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    prices = raw["prices"]
    names = raw["name_match"]

    config = ContactsConfig(
        company_usd=Decimal(str(prices["company_usd"])),
        company_run_start_usd=Decimal(str(prices["company_run_start_usd"])),
        person_with_email_usd=Decimal(str(prices["person_with_email_usd"])),
        max_people_per_lead=int(raw["max_people_per_lead"]),
        max_short_length=int(names["max_short_length"]),
        # 输入   ['air', 'Fever ', ...]
        # 输出   frozenset({'air', 'fever', ...})。跟规范化之后的公司名比，所以统一小写去空白
        common_words=frozenset(str(w).strip().lower() for w in names["common_words"]),
        homepage_workers=int(raw["homepage_workers"]),
        # 输入   ['linktr.ee', 'Instagram.com', ...]
        # 输出   frozenset({'linktr.ee', 'instagram.com', ...})。跟取出来的域名比，统一小写去空白
        shared_hosts=frozenset(str(h).strip().lower() for h in raw["shared_hosts"]),
    )

    if config.max_people_per_lead < 0:
        raise ValueError(f"max_people_per_lead 不能是负数，拿到 {config.max_people_per_lead}")
    if config.homepage_workers < 1:
        # 0 个线程的线程池会直接报错，而且报在抓官网那一步、离配置很远
        raise ValueError(f"homepage_workers 至少是 1，拿到 {config.homepage_workers}")
    return config


@dataclass(frozen=True)
class LookupPlan:
    """这一批要花钱查什么。plan_lookups 算出来，find_contacts 照着做。

    两格：

        companies   C1（手上有 LinkedIn 公司 id）、没抓过、要抓公司详情的：公司 id -> lead_id
        people      P3 / P4（个人、职位串里有这家的名字）要花钱查的人：
                    主页链接（已经去掉 ?miniProfileUrn= 那段）-> [(替哪家查, 凭的哪个作者), ...]
                    同一个人出现在两家的证据里，只查一次、查回来两家各判一次（对上是 P3 / P4，对不上是 P9）

    一个实际值：文件开头那一批八家（每种情况一家），以前一家都没抓过、一个人都没查过：

        LookupPlan(
            companies={
                "200000014": 1285,     # Boomcrate               C1
                "200000010": 1286,    # BVF Georgia            C1
                "100000001": 1289,    # North Field Technologies  C1
                "200000011": 1334,     # Makers&Co          C1
                "100000005": 1341,     # Earmark                C1
                "200000015": 1354,     # Air                    C1
                "100000002": 1388,     # Hushly                 C1
            },                        # 没有 Chatterly Inc.（1281）：C2，只有名字，不抓
            people={
                # P3  Chatterly Inc. 的创始人，通道 headline
                "https://www.linkedin.com/in/rshah": [
                    (1281, AuthorRef(
                        signal_id=2714, role="poster", name="Ravi Shah",
                        headline="Founder/CEO Chatterly Inc. | Safe-listening headphones | Offset Audioᵀᴹ",
                        url="https://www.linkedin.com/in/rshah?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAA...",
                        is_company_page=False,
                        post_url="https://www.linkedin.com/posts/rshah_chatterly-inc-activity-7495964008185745408-MXaA",
                        event_at=datetime(2026, 8, 19, 22, 4, 47, tzinfo=UTC))),
                ],
                # P6 里原帖作者那一半，落进 P3 那种：Arthur Mann 转发了他的帖子，
                # 他的职位串写着 Boomcrate 的 CEO & Founder。
                # post_url 是 Arthur 转发的那条（我们存的是 signal 2718 那条转发，原帖没有单独一行）。
                # 同一条 signal 2718 也是 BVF Georgia 的证据，但替 BVF Georgia 判他是 P5（职位串里没有
                # BVF Georgia），所以这里只有 1285 一对，没有 1286
                "https://www.linkedin.com/in/rob-olsen-100000001": [
                    (1285, AuthorRef(
                        signal_id=2718, role="original_poster", name="Rob Olsen",
                        headline="CEO & Founder, Managing Partner & Celebrity Relations at Boomcrate",
                        url="https://www.linkedin.com/in/rob-olsen-100000001?miniProfileUrn=urn%3Ali%3A...",
                        is_company_page=False,
                        post_url="https://www.linkedin.com/posts/arthur-mann_bvf-team-bvf-georgia-...-2y0a",
                        event_at=datetime(2026, 8, 11, 9, 38, 44, tzinfo=UTC))),
                ],
                # P6 里转发的人那一半，落进 P3 那种：他转发了 SingAlong Group 的帖子
                # （SingAlong Group 是原帖作者那一半，P2，不查）
                "https://www.linkedin.com/in/malavoie": [
                    (1289, AuthorRef(
                        signal_id=2720, role="poster", name="Marc-Andre Lavoie",
                        headline="Founder & CEO, North Field Technologies | Seratone is a ...",
                        url="https://www.linkedin.com/in/malavoie?miniProfileUrn=urn%3Ali%3Afsd_profile%3A...",
                        is_company_page=False,
                        post_url="https://www.linkedin.com/posts/activity-7493771291343237120-wIy1",
                        event_at=datetime(2026, 8, 13, 13, 30, 8, tzinfo=UTC))),
                ],
                # P4  Hushly 的创始人，通道 mentioned（他在自己的帖子里 @ 了 Hushly）
                "https://www.linkedin.com/in/sam-hadid-a1b2c3d4": [
                    (1388, AuthorRef(
                        signal_id=2820, role="poster", name="sam hadid",
                        headline="ceo @ Hushly - a sanctuary for human sound",
                        url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4?miniProfileUrn=urn%3Ali%3A...",
                        is_company_page=False,
                        post_url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_in-the-vault-...-pmIJ",
                        event_at=datetime(2026, 9, 21, 19, 56, 31, tzinfo=UTC))),
                ],
            },
        )

    people 里没有的作者，各是哪一种：

        P1  Makers&Co 自己的主页（signal 2784）
        P2  audioXpress 的主页（Earmark，signal 2789）、SingAlong Group 的主页（North Field，signal 2720 的原帖）、
            BVF Team 的主页（Boomcrate，signal 2830 的原帖）
        P5  Arthur Mann（Boomcrate，signal 2718 / 2748）、Ana Petrosyan（Boomcrate，signal 2830）、
            Rob Olsen（替 BVF Georgia 判，signal 2718 的原帖）
        P7  Arthur Mann（替 BVF Georgia 判，signal 2718）
        P8  Luz Cortez（Air，signal 2801）

    照这个计划报价：7 x 0.004 + 0.00005 + 4 x 0.01 = 0.06805
    """

    companies: dict[str, int]
    people: dict[str, list[tuple[int, AuthorRef]]]


def plan_lookups(
    inputs: dict[int, tuple[LeadForContacts, list[AuthorRef]]],
    *,
    profiles: dict[int, CompanyProfile],
    lookups: dict[int, list[SavedPersonLookup]],
    config: ContactsConfig,
) -> LookupPlan:
    """这一批里哪几家要抓公司详情（C1）、哪几个人要花钱查（P3 / P4）。纯函数，不连库不连网。

    输入：
        inputs    contacts_db.contact_inputs 的输出：每家的身份和作者（作者已经按帖子从新到旧排）
            {1388: (LeadForContacts(lead_id=1388, display_name="Hushly", normalized_name="hushly",
                                    identity_kind="li_id", linkedin_company_id="100000002",
                                    linkedin_slug="hushly", domain=None),
                                [AuthorRef(signal_id=2820, role="poster", name="sam hadid",
                                            headline="ceo @ Hushly - a sanctuary for human sound",
                                            url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4"
                                                "?miniProfileUrn=urn%3Ali%3Afsd_profile%3A"
                                                "ACoAAAhF1l8BtzHcOI5FyDUR06aqDMwXVtCQXik",
                                            is_company_page=False,
                                            post_url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_"
                                                    "in-the-vault-is-on-its-way-to-becoming-one-"
                                                    "activity-7507890528395587584-pmIJ",
                                            event_at=datetime(2026, 9, 21, 19, 56, 31, 830000, tzinfo=UTC))])}
        profiles  以前抓过的概况，如 {1341: CompanyProfile(...)}，抓过的这次不再抓
        lookups   以前查过的人，如 {1388: [SavedPersonLookup(sam hadid ...)]}，查过的这次不再查
        config    每家最多查几个人；P8（名字太短或是常用词）用的那两条
    输出：LookupPlan，例子见那个类

    一个例子（max_people_per_lead=2）：

        输入   Hushly 的证据作者依次是 sam hadid（P4）、某媒体的主页（P2）、
               sam hadid 又一条（P4，同一个人）；以前没查过人
        输出   people 里只有 sam hadid 一个：同一个人第二条证据不再占一个名额
    """
    companies: dict[str, int] = {}
    people: dict[str, list[tuple[int, AuthorRef]]] = {}
    
    # 对于这一批的每一家公司
    for lead_id, (lead, authors) in inputs.items():
        # 总结出需要做C1的leads：若有linkedin_company_id 且 之前没抓过 -> 要抓公司详情
        # 总结出需要做C2的leads：lead只有名字的时候linkedin_company_id 是 None，不进这里
        if lead.linkedin_company_id and lead_id not in profiles:
            companies[lead.linkedin_company_id] = lead_id

        # 总结出以前对于该lead查过哪些people
        # 这里拿主页链接当键过一遍 clean_profile_url：
        # 虽然库里存的已经是干净的，再过一遍是防哪天有人不经过这里直接写库
        # done = {'https://www.linkedin.com/in/sam-hadid-a1b2c3d4', ...}：之前为该lead查过的people
        done = {
            harvestapi_profile.clean_profile_url(s.lookup.person_linkedin_url)
            for s in lookups.get(lead_id, [])
        }

        # 这一次还能查几个people。以前查过 2 个就是 0（max_people_per_lead=2 时）
        room = config.max_people_per_lead - len(done)
        
        # 对于该lead的每一个作者（AuthorRef）
        # 一家公司在 inputs 里是一个 LeadForContacts（身份）加一个作者列表 authors；
        # 每个 AuthorRef 是这家某一条证据（signal）里的一个作者，转发的帖子一条证据出两个
        for author in authors:
            if room <= 0:
                break
            # 若该authorref是P1或P2的情况，即作者是公司主页：不查人
            if author.is_company_page:
                continue
            # 使用 classify_author 分类
            kind = classify_author(
                lead, author, common_words=config.common_words, max_short_length=config.max_short_length
            )
            # NAMED_IN_HEADLINE代表 P3/P4，就是我们要查的那一类，如果不是，就放弃
            if kind is not AuthorKind.NAMED_IN_HEADLINE:
                continue

            # 如果是 P3/P4，对author.url做清理
            key = harvestapi_profile.clean_profile_url(author.url)
            # 这次调查以前就查过，或者，该author在同一个lead里出现过两次，就不再占名额
            if key in done:
                continue
            # 列为已出现过
            done.add(key)
            room -= 1

            # 把合格的Author加入需要被调查的people的行列
            people.setdefault(key, []).append((lead_id, author))


    # 最后需要做C1调查的companies形如：
    # companies={"200000014": 1285,...}            
    # 最后需要做P3和P4调查的people形如：            
    # people={
        # "https://www.linkedin.com/in/rshah": [
        # (1281, AuthorRef(
        #  signal_id=2714, role="poster", name="Ravi Shah",
        #  headline="Founder/CEO Chatterly Inc. | Safe-listening headphones | Offset Audioᵀᴹ",
        #  url="https://www.linkedin.com/in/rshah?miniProfileUrn=urn%3Ali%3Afsd_profile%3AACoAA...",
        #  is_company_page=False,
        #  post_url="https://www.linkedin.com/posts/rshah_chatterly-inc-activity-7495964008185745408-MXaA",
        #  event_at=datetime(2026, 8, 19, 22, 4, 47, tzinfo=UTC))),
        # ],...}
    return LookupPlan(companies=companies, people=people)


def quote_usd(plan: LookupPlan, config: ContactsConfig) -> Decimal:
    """照计划报价，按价格表往高了报。纯函数。

    输入：LookupPlan；单价
    输出：Decimal，三个例子：

        2 家公司、2 个人   2 x 0.004 + 0.00005 + 2 x 0.01 = Decimal("0.02805")
        0 家公司、1 个人   0.01                             = Decimal("0.01")
                           没有公司就不起那一次 run，不收启动费
        什么都不用查                                        = Decimal("0")

    按「找到邮箱」的价报：找没找到要查了才知道，没找到只收 0.004，所以实际只会少不会多
    """
    total = Decimal("0")
    if plan.companies:
        total += config.company_usd * len(plan.companies) + config.company_run_start_usd
    total += config.person_with_email_usd * len(plan.people)
    return total


@dataclass
class ContactsReport:
    """find_contacts 做完的账，调用方拿它打日志、拼回给用户的话。

    一个实际值（Hushly、Chatterly Inc.、Earmark 那一批）：

        ContactsReport(lead_count=3, companies_fetched=2, people_looked_up=2, people_matched=2,
                       emails_from_people=1, emails_from_websites=2, spent_usd=Decimal("0.02205"),
                       blocked_by_cap=None, homepage_errors={}, shared_hosts={}, actor_errors={},
                       domain_conflicts={}, changed_emails={}, missing={})

        companies_fetched   C1 抓到了几家的公司详情
        people_looked_up    P3 / P4 花钱查了几个人（actor 真返回了的）
        people_matched      其中几个现职对得上这家；查了但没对上的是 P9（现职不在这家）
        spent_usd           这一次真扣的钱：公司详情 0.00805 + 查人 0.014 = 0.02205
                            （比报价少，因为 Ravi 没找到邮箱）
        blocked_by_cap      超了月度上限没找的原因，那句话原样；没超是 None
        homepage_errors     官网打不开的：lead_id -> 原因，如 {1341: "HTTP 403"}
        shared_hosts        官网是共用平台、不当成这家域名的：lead_id -> 那个域名，如 {1290: "linktr.ee"}
        actor_errors        调 actor 出错、那一步跳过了的：哪一步 -> 错误，如
                            {"company_profile": "RuntimeError: run abc 结束状态为 FAILED"}
        domain_conflicts    官网域名已经在另一家那一行上的：lead_id -> 那一家的 lead_id
        changed_emails      以前有邮箱、这一次又找到新地址的：lead_id -> (新地址, 以前的地址)，
                            如 {1388: (["sam@hushly.example"], ["contact@hushly.example"])}
        missing             付了钱但 actor 没返回的：公司 id 或主页链接 -> 替哪家查的
    """

    lead_count: int = 0
    companies_fetched: int = 0
    people_looked_up: int = 0
    people_matched: int = 0
    emails_from_people: int = 0
    emails_from_websites: int = 0
    spent_usd: Decimal = Decimal("0")
    blocked_by_cap: str | None = None
    homepage_errors: dict[int, str] = field(default_factory=dict)
    shared_hosts: dict[int, str] = field(default_factory=dict)
    actor_errors: dict[str, str] = field(default_factory=dict)
    domain_conflicts: dict[int, int] = field(default_factory=dict)
    changed_emails: dict[int, tuple[list[str], list[str]]] = field(default_factory=dict)
    missing: dict[str, int] = field(default_factory=dict)


def find_contacts_for_run(conn: Any, *, run_id: int, now: dt.datetime) -> ContactsReport:
    """这一跑要推卡的那几家，找联系方式。两条路都调这一个，挑哪几家的口径只在这里。

    输入：连接（上面不能有没提交的写）；哪一跑，如 45；这一刻，带时区
    输出：ContactsReport，例子见那个类

    挑哪几家跟推卡的口径一样（见 contacts_db.leads_to_contact）：这一跑打了分、不在写稿的；
    config/discovery.yaml 的 push_below_line 关着时只挑过线的（没过线的不推卡，找了白花钱）

        push_below_line=True    这一跑分析过的全部（在写稿的除外）
        push_below_line=False   只有 score >= qualified_score 的那几家
    """
    # 输出   CardConfig(qualified_score=60, push_below_line=True, ...)，跟推卡那一步读的是同一份
    card_config = load_card_config()
    min_score = None if card_config.push_below_line else card_config.qualified_score

    # 输出   [1285, 1289, 1388]
    with conn.cursor() as cur:
        lead_ids = contacts_db.leads_to_contact(cur, run_id, min_score=min_score)
    # 查完这一句没有写，但事务开着；提交掉，find_contacts 要求连接上干干净净
    conn.commit()

    print(f"[contacts] run {run_id} 要推卡的 {len(lead_ids)} 家：{lead_ids}")
    return find_contacts(conn, lead_ids, run_id=run_id, now=now)


def find_contacts(
    conn: Any, lead_ids: Sequence[int], *, run_id: int | None, now: dt.datetime
) -> ContactsReport:
    """给这一批要推的公司找联系方式，结果写进库。会花钱，自己提交。

    输入：
        conn      数据库连接（不是游标：这个函数要在中间提交），上面不能有没提交的写
        lead_ids  这一批要推卡片的公司，如 [1388, 1281, 1341]
        run_id    哪一跑触发的，如 45，写进每一行的 run_id
        now       这一刻，带时区，写进每一行的时间
    输出：ContactsReport，例子见那个类

    六步和为什么自己提交，见文件开头
    """
    config = load_contacts_config()
    report = ContactsReport(lead_count=len(set(lead_ids)))
    if not lead_ids:
        return report

    # ① 读原料。四句查询在同一个游标上，只读不写。
    #    下面的例子：lead_ids=[1388, 1289]（Hushly、North Field Technologies）
    with conn.cursor() as cur:
        # inputs：这几家的身份，和每条证据里的作者
        #   从库里哪儿来：leads 查身份；lead_evidence 按 lead_id 找到这家的证据，
        #                 再按 signal_id 连到 signals 取作者（转发的帖子多取 reposted_actor_* 那三列）
        #   只有 leads 里真有的 lead_id 才会出现在 inputs 里，后面三句都拿 inputs 的键去查
        #   输出
        #   {1388: (LeadForContacts(lead_id=1388, display_name="Hushly", normalized_name="hushly",
        #                           identity_kind="li_id", linkedin_company_id="100000002",
        #                           linkedin_slug="hushly", domain=None),
        #           [AuthorRef(signal_id=2820, role="poster", name="sam hadid",
        #                      headline="ceo @ Hushly - a sanctuary for human sound",
        #                      url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4"
        #                          "?miniProfileUrn=urn%3Ali%3Afsd_profile%3A"
        #                          "ACoAAAhF1l8BtzHcOI5FyDUR06aqDMwXVtCQXik",
        #                      is_company_page=False,
        #                      post_url="https://www.linkedin.com/posts/sam-hadid-a1b2c3d4_"
        #                               "in-the-vault-is-on-its-way-to-becoming-one-"
        #                               "activity-7507890528395587584-pmIJ",
        #                      event_at=datetime(2026, 9, 21, 19, 56, 31, 830000, tzinfo=UTC))]),
        #    1289: (LeadForContacts(lead_id=1289, display_name="North Field Technologies",
        #                           normalized_name="north field technologies", identity_kind="li_id",
        #                           linkedin_company_id="100000001", linkedin_slug="northfield-inc",
        #                           domain=None),
        #           [AuthorRef(signal_id=2720, role="poster", name="Marc-Andre Lavoie",
        #                      headline="Founder & CEO, North Field Technologies | "
        #                               "Seratone is a plug-and-play karaoke that turns any "
        #                               "speaker into a studio | Patent-pending, "
        #                               "zero-latency audio | Kickstarter 2026",
        #                      url="https://www.linkedin.com/in/malavoie"
        #                          "?miniProfileUrn=urn%3Ali%3Afsd_profile%3A"
        #                          "ACoAABJk2n4BYinVAjs6e2JRndhwR-ejsQb6D-0",
        #                      is_company_page=False,
        #                      post_url="https://www.linkedin.com/posts/activity-7493771291343237120-wIy1",
        #                      event_at=datetime(2026, 8, 13, 13, 30, 8, 439000, tzinfo=UTC)),
        #            AuthorRef(signal_id=2720, role="original_poster", name="SingAlong Group",
        #                      headline="2,244 followers",
        #                      url="https://www.linkedin.com/company/singalong-group/posts",
        #                      is_company_page=True,
        #                      post_url="https://www.linkedin.com/posts/activity-7493771291343237120-wIy1",
        #                      event_at=datetime(2026, 8, 13, 13, 30, 8, 439000, tzinfo=UTC))])}
        #   North Field 那条是转发的帖子（P6），一条 signal 出两个作者：转发的人和原帖作者，
        #   两个 AuthorRef 的 signal_id、post_url、event_at 都是那一条转发的
        inputs = contacts_db.contact_inputs(cur, lead_ids)

        # 后面三句查的是「以前找联系方式时写进去的东西」，决定这一次哪些可以跳过、不再花钱。
        # 这两家一次都还没找过，所以这一次三句都是 {}。下面的例子是 Hushly 找过一次之后、
        # 第二次再推它时读回来的样子（第一次写进去的那几行）

        # profiles：每家最新的一份公司概况。有的就不再抓公司详情（C1 抓过不再抓），⑥ 拿它的官网地址
        #   从库里哪儿来：company_profiles，按 lead_id 查，同一家有几行时取 fetched_at 最新的那一行
        #   输出
        #   {1388: CompanyProfile(website="http://www.hushly.example", employee_count=11,
        #                         employee_count_range="2-10", founded_year=2022, location="United States",
        #                         industries=("Music",), phone=None,
        #                         raw=<actor 返回的那一整条 dict，有 id、name、website、employeeCount、
        #                              employeeCountRange、foundedOn、locations、industries 等 32 个键>)}
        #   North Field 没抓过，不在里面
        profiles = contacts_db.latest_profiles(cur, list(inputs))

        # lookups：每家以前花钱查过的人。查过的这次不再查，而且占 max_people_per_lead 的名额
        #   从库里哪儿来：person_lookups，按 lead_id 查，按查的先后排
        #   输出
        #   {1388: [SavedPersonLookup(
        #              id=<那一行的主键>, signal_id=2820,
        #              lookup=PersonLookup(
        #                  person_linkedin_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
        #                  person_name="sam hadid",
        #                  headline="ceo @ Hushly - a sanctuary for human sound",
        #                  current_company_name="Hushly",
        #                  current_company_linkedin_url="https://www.linkedin.com/company/hushly/",
        #                  matches_lead=True,
        #                  raw=<个人主页 actor 返回的那一整条 dict>),
        #              fetched_at=<第一次查他的时刻>)]}
        #   person_linkedin_url 是去掉 ?miniProfileUrn= 之后的，跟 inputs 里 url 那一格不一样长
        lookups = contacts_db.person_lookups_for(cur, list(inputs))

        # existing：每家以前找到的联系方式，最新的在前。以前从官网找到过邮箱的，⑥ 就不再抓官网
        #   从库里哪儿来：lead_contacts，按 lead_id 查，按 found_at 从新到旧排
        #   输出
        #   {1388: [SavedContact(
        #              id=<那一行的主键>,
        #              contact=FoundContact(channel=ContactChannel.EMAIL, value="contact@hushly.example",
        #                                   source=ContactSource.WEBSITE, source_url="https://home.hushly.example",
        #                                   email_status=None, catch_all=None, found_in_google=None,
        #                                   quality_score=None),
        #              person_lookup_id=None,
        #              found_at=<第一次找到的时刻>),
        #           SavedContact(
        #              id=<那一行的主键>,
        #              contact=FoundContact(channel=ContactChannel.EMAIL, value="sam@hushly.example",
        #                                   source=ContactSource.LINKEDIN_EMAIL_SEARCH,
        #                                   source_url="https://www.linkedin.com/in/sam-hadid-a1b2c3d4",
        #                                   email_status="risky", catch_all=True, found_in_google=True,
        #                                   quality_score=75),
        #              person_lookup_id=<上面 lookups 里 sam hadid 那一行的 id>,
        #              found_at=<第一次找到的时刻>)]}
        #   官网来的那一条四样可信度全空、person_lookup_id 空；个人主页来的那一条指回查人那一行
        existing = contacts_db.contacts_for(cur, list(inputs))

    # 这一次开始之前每家已经有的邮箱，⑤ ⑥ 找到新地址时拿它来比，决定要不要记进 report.changed_emails
    # known = {1388: ["contact@hushly.example", "sam@hushly.example"]}，按 existing 的顺序（新的在前），去重。
    emails_before: dict[int, list[str]] = {}
    for lead_id, saved_list in existing.items():
        for saved in saved_list:
            # 用 == 不用 is：ContactChannel 是 StrEnum，跟字符串 'email' 用 == 比也相等
            if saved.contact.channel == ContactChannel.EMAIL:
                known = emails_before.setdefault(lead_id, [])
                if saved.contact.value.lower() not in {v.lower() for v in known}:
                    known.append(saved.contact.value)

    # ② 定计划
    # 输出   LookupPlan(companies={'100000002': 1388, ...}, people={'https://www.linkedin.com/in/...': [...]})
    plan = plan_lookups(inputs, profiles=profiles, lookups=lookups, config=config)
    # 计算花销
    quote = quote_usd(plan, config)
    print(
        f"[contacts] {report.lead_count} 家：抓公司详情 {len(plan.companies)} 家、"
        f"查人 {len(plan.people)} 个，报价 ${quote}"
    )

    # ③ 过闸。
    # 如果plan为空，那quote为0，就什么都不用查
    if quote > 0:
        try:
            with conn.cursor() as cur:
                # 该方法会比对quote和月度上限
                check_monthly_cap(
                    cur,
                    estimate_usd=float(quote),
                    now=now,
                    budget=load_budget_config(),
                    unit_price_usd=load_planner_config().unit_price_usd,
                )
        # 若quote超过月度上限，则report.blocked_by_cap会被写入
        except MonthlyCapReached as err:
            report.blocked_by_cap = str(err)
            print(f"[contacts] 超了月度上限，不找付费的那一半：{err}")

    # 若blocked_by_cap为空，则说明quote低于月度上限，开始按计划检查
    if report.blocked_by_cap is None:
        # ④ 公司详情
        # 调 actor 出错（Apify 挂了、run 失败、超时）不往外抛：记进 report.actor_errors，这一步跳过，
        # 后面照做、卡片照推。往外抛的话推卡那一步跟着崩，这一批一张卡都发不出去。
        # rollback 是因为出错时可能停在一个没提交的事务里（记账那一笔已经单独提交过了，不受影响）
        if plan.companies:
            try:
                _fetch_companies(
                    conn, plan, profiles=profiles, run_id=run_id, now=now, config=config, report=report
                )
            except Exception as err:
                conn.rollback()
                report.actor_errors["company_profile"] = f"{type(err).__name__}: {err}"
                print(f"[contacts] 公司详情这一步出错，跳过：{type(err).__name__}: {err}")
        # ⑤ 查人，出错的处理同上
        if plan.people:
            try:
                _look_up_people(
                    conn,
                    plan,
                    inputs=inputs,
                    emails_before=emails_before,
                    run_id=run_id,
                    now=now,
                    config=config,
                    report=report,
                )
            except Exception as err:
                conn.rollback()
                report.actor_errors["person_email"] = f"{type(err).__name__}: {err}"
                print(f"[contacts] 查人这一步出错，跳过：{type(err).__name__}: {err}")

    # ⑥ 官网，不花钱
    _read_homepages(
        conn,
        inputs,
        profiles=profiles,
        existing=existing,
        emails_before=emails_before,
        run_id=run_id,
        now=now,
        config=config,
        report=report,
    )

    # 邮箱变了的那几家，一家一行，等人看
    # 输出   [contacts] lead=1388 以前有 contact@hushly.example，这次又找到 sam@hushly.example，新旧都留着，请人看一眼
    for lead_id, (new, old) in report.changed_emails.items():
        print(
            f"[contacts] lead={lead_id} 以前有 {'、'.join(old)}，这次又找到 {'、'.join(new)}，"
            "新旧都留着，请人看一眼"
        )

    print(
        f"[contacts] 完成：公司详情 {report.companies_fetched} 家，查人 {report.people_looked_up} 个"
        f"（对上 {report.people_matched} 个），"
        f"邮箱：个人主页 {report.emails_from_people} 个、官网 {report.emails_from_websites} 个，"
        f"花了 ${report.spent_usd}"
    )
    return report


def _fetch_companies(
    conn: Any,
    plan: LookupPlan,
    *,
    profiles: dict[int, CompanyProfile],
    run_id: int | None,
    now: dt.datetime,
    config: ContactsConfig,
    report: ContactsReport,
) -> None:
    """④ C1（手上有 LinkedIn 公司 id）那几家：一次 run 抓公司详情，先记账提交，再存概况提交。

    输入：plan.companies，如 {'100000003': 1295, '100000004': 1287}（Bonewave、ORBO Audio）
    输出：无。库里的变化（这两家一次 run 的真结果）：

        actor_spend       +1 行  actor='harvestapi/linkedin-company'  purpose='company_profile'
                                 item_count=2  cost_usd=0.00805  lead_id=NULL（一次查了两家）   -> 提交
        company_profiles  +1 行  lead_id=1295  website='https://bonewave.example/'  employee_count=16
                                 employee_count_range='201-500'  founded_year=NULL
                                 location='Suzhou, People's Republic of China'
                                 industries={'Appliances, Electrical, and Electronics Manufacturing'}
                          +1 行  lead_id=1287  website='http://orbobrand.example'  employee_count=7
                                 employee_count_range='2-10'  founded_year=2023
                                 location='Euless, TX, United States'  industries={'Consumer Electronics'}
                                                                                                 -> 提交

    内存里：profiles 补上 1295、1287 两家（⑥ 拿它的 website 去抓官网）。
    actor 没返回的那一家（公司页被删了、id 不对）不写 company_profiles，记进 report.missing，
    如 {'100000003': 1295}，下次还会再抓
    """
    # 输入   ['100000003', '100000004']
    # 输出   ActorRun(items=[Bonewave 的原始返回, ORBO Audio 的原始返回], cost_usd=Decimal("0.00805"),
    #                 charged_events={"apify-actor-start": 1, "apify-default-dataset-item": 2})
    run = harvestapi_company.fetch_companies(list(plan.companies))

    # Apify 没回扣费（cost_usd=None）就按价格表记一笔：不记的话月度上限漏了这一次
    cost = run.cost_usd
    if cost is None:
        cost = config.company_usd * len(plan.companies) + config.company_run_start_usd
        print(f"[contacts] Apify 没回公司详情这一次的扣费，按价格表记 ${cost}")

    with conn.cursor() as cur:
        contacts_db.record_spend(
            cur,
            actor=_COMPANY_ACTOR,
            purpose=SpendPurpose.COMPANY_PROFILE,
            item_count=len(run.items),
            cost_usd=cost,
            run_id=run_id,
            # 只查了一家就归到那一家，查了好几家是 NULL
            lead_id=next(iter(plan.companies.values())) if len(plan.companies) == 1 else None,
            called_at=now,
        )
    conn.commit()
    report.spent_usd += cost

    # 按 item 的 id 对回是哪家（返回的顺序不保证跟传进去的一样）
    # 输入   item {"id": "100000003", ...}，plan.companies {'100000003': 1295, ...}
    # 输出   company_profiles 多一行 lead_id=1295；profiles[1295] = 那份概况
    returned: set[str] = set()
    with conn.cursor() as cur:
        for item in run.items:
            company_id = harvestapi_company.item_company_id(item)
            lead_id = plan.companies.get(company_id)
            if lead_id is None:
                # 返回了一家我们没问的（不该出现）。不存：挂到哪家头上都是猜
                print(f"[contacts] 公司详情多返回了一家没问过的 id={company_id}，跳过")
                continue
            profile = harvestapi_company.to_profile(item)
            contacts_db.insert_company_profile(cur, lead_id, profile, run_id=run_id, fetched_at=now)
            profiles[lead_id] = profile
            returned.add(company_id)
            report.companies_fetched += 1
    conn.commit()

    for company_id, lead_id in plan.companies.items():
        if company_id not in returned:
            report.missing[company_id] = lead_id
            print(f"[contacts] 公司详情没返回 lead={lead_id} 公司 id={company_id}")


def _look_up_people(
    conn: Any,
    plan: LookupPlan,
    *,
    inputs: dict[int, tuple[LeadForContacts, list[AuthorRef]]],
    emails_before: dict[int, list[str]],
    run_id: int | None,
    now: dt.datetime,
    config: ContactsConfig,
    report: ContactsReport,
) -> None:
    """⑤ P3 / P4（个人、职位串里有这家的名字）那几个人：一次 run 查主页和邮箱，
    先记账提交，再逐人核对现职、存结果提交。

    输入：plan.people，如 {'https://www.linkedin.com/in/sam-hadid-a1b2c3d4': [(1388, AuthorRef(...))]}
    输出：无。库里的变化（sam hadid 和 Ravi Shah 一次 run 的真结果）：

        actor_spend      +1 行  actor='harvestapi/linkedin-profile-scraper'  purpose='person_email'
                                item_count=2  cost_usd=0.01400  lead_id=NULL（一次查了两个人）
                                sam 找到邮箱收 0.01，Ravi 没找到只收 0.004                    -> 提交
        person_lookups   +1 行  lead_id=1388  signal_id=2820
                                person_linkedin_url='https://www.linkedin.com/in/sam-hadid-a1b2c3d4'
                                current_company_name='Hushly'
                                current_company_linkedin_url='https://www.linkedin.com/company/hushly/'
                                matches_lead=true
                         +1 行  lead_id=1281  signal_id=2714
                                person_linkedin_url='https://www.linkedin.com/in/rshah'
                                current_company_name='Chatterly Inc.'  current_company_linkedin_url=NULL
                                matches_lead=true
        lead_contacts    +1 行  lead_id=1388  channel='email'  value='sam@hushly.example'
                                source='linkedin_email_search'
                                source_url='https://www.linkedin.com/in/sam-hadid-a1b2c3d4'
                                person_lookup_id=<上面 sam 那一行的 id>
                                email_status='risky'  catch_all=true  found_in_google=true  quality_score=75
                                Ravi 没找到邮箱，他那家不多行                                  -> 提交

    怎么判「对不对得上」、对不上（P9）时库里怎么变，见文件开头 P3 和 P9
    """
    # 输入   ['https://www.linkedin.com/in/sam-hadid-a1b2c3d4', 'https://www.linkedin.com/in/rshah']
    # 输出   ActorRun(items=[两个人的原始返回], cost_usd=Decimal("0.01400"),
    #                 charged_events={"profile": 1, "profile_with_email": 1})
    run = harvestapi_profile.fetch_profiles(list(plan.people))

    cost = run.cost_usd
    if cost is None:
        cost = config.person_with_email_usd * len(plan.people)
        print(f"[contacts] Apify 没回查人这一次的扣费，按价格表记 ${cost}")

    with conn.cursor() as cur:
        # 只替一家查了一个人就归到那一家，否则是 NULL
        only = next(iter(plan.people.values())) if len(plan.people) == 1 else []
        contacts_db.record_spend(
            cur,
            actor=_PROFILE_ACTOR,
            purpose=SpendPurpose.PERSON_EMAIL,
            item_count=len(run.items),
            cost_usd=cost,
            run_id=run_id,
            lead_id=only[0][0] if len(only) == 1 else None,
            called_at=now,
        )
    conn.commit()
    report.spent_usd += cost

    # 按我们传进去的链接对回是谁
    # 输出   {'https://www.linkedin.com/in/sam-hadid-a1b2c3d4': {原始返回}, ...}
    by_url = {harvestapi_profile.item_request_url(item): item for item in run.items}

    with conn.cursor() as cur:
        for url, pairs in plan.people.items():
            item = by_url.get(url)
            if item is None:
                # 主页删了、链接不对：不写 person_lookups，下次还会再查
                report.missing[url] = pairs[0][0]
                print(f"[contacts] 个人主页没返回 {url}")
                continue

            # 输出   ScrapedPerson(name='sam hadid', current_jobs=(CurrentJob(Hushly ...),),
            #                      emails=(FoundContact(sam@hushly.example ...),), ...)
            person = harvestapi_profile.to_person(item)
            report.people_looked_up += 1

            for lead_id, author in pairs:
                lead = inputs[lead_id][0]

                # 输出   对得上 (CurrentJob(company_name='Hushly', company_id='100000002', ...), True)；
                #        对不上（P9）(他现在那家, False)
                job, matches = pick_current_job(lead, person.current_jobs)
                lookup = PersonLookup(
                    person_linkedin_url=url,
                    person_name=person.name or author.name,
                    # 记的是我们当时凭它决定去查的那一行，也就是帖子上写的职位串
                    headline=author.headline,
                    current_company_name=job.company_name if job else None,
                    current_company_linkedin_url=job.company_linkedin_url if job else None,
                    matches_lead=matches,
                    raw=item,
                )
                # 输出   (新那一行的 id, True)；崩溃之后重跑撞上唯一约束是 (旧那一行的 id, False)
                lookup_id, _ = contacts_db.insert_person_lookup(
                    cur, lead_id, lookup, signal_id=author.signal_id, run_id=run_id, fetched_at=now
                )

                # P9：person_lookups 已经写了，邮箱不写
                if not matches:
                    print(
                        f"[contacts] lead={lead_id} {lead.display_name}：{lookup.person_name} "
                        "职位串写着这家，"
                        f"现职在 {lookup.current_company_name}，邮箱不挂"
                    )
                    continue
                report.people_matched += 1

                # 对得上：他的每个邮箱写一行 lead_contacts，指回上面那一行
                # 输入   person.emails=(FoundContact(value='sam@hushly.example', email_status='risky', ...),)
                for email in person.emails:
                    if contacts_db.insert_contact(
                        cur, lead_id, email, person_lookup_id=lookup_id, run_id=run_id, found_at=now
                    ):
                        report.emails_from_people += 1
                        _note_changed_email(report, emails_before, lead_id, email.value)
    conn.commit()


def _read_homepages(
    conn: Any,
    inputs: dict[int, tuple[LeadForContacts, list[AuthorRef]]],
    *,
    profiles: dict[int, CompanyProfile],
    existing: dict[int, list[SavedContact]],
    emails_before: dict[int, list[str]],
    run_id: int | None,
    now: dt.datetime,
    config: ContactsConfig,
    report: ContactsReport,
) -> None:
    """⑥ C1（手上有 LinkedIn 公司 id）那几家：打开官网首页，找域名跟官网一样的邮箱，
    顺手把官网跳转之后落到的那个地址的域名补进 leads.domain。不花钱。

    输入：这一批的公司；每家的概况（④ 刚补上的也在里面）；以前找到的联系方式
    输出：无。库里的变化（Hushly、ORBO Audio 两家官网首页的真结果）：

        lead_contacts  +1 行  lead_id=1388  channel='email'  value='contact@hushly.example'  source='website'
                              source_url='https://home.hushly.example'（跳转之后落到的那一页）
                              person_lookup_id=NULL  四样可信度全是 NULL
                       +1 行  lead_id=1287  channel='email'  value='mark@orboaudio.example'  source='website'
                              source_url='https://orboaudio.example'  person_lookup_id=NULL
        leads          改 1 格  lead 1388  domain  NULL -> 'hushly.example'
                       改 1 格  lead 1287  domain  NULL -> 'orboaudio.example'
                                （不是 LinkedIn 上登记的 orbobrand.example）
                                                                                               -> 提交

    抓哪几家：有概况、概况里有官网地址、以前没从官网找到过邮箱的（Chatterly 是 C2，没有概况，不抓）。
    官网打不开的什么都不写，记进 report.homepage_errors，如 {1341: 'HTTP 403'}；
    域名已经在别家那一行上的不写，记进 report.domain_conflicts，如 {1388: 1288}
    """
    # 输出   {1388: 'http://www.hushly.example', 1341: 'https://earmark.example'}
    targets: dict[int, str] = {}
    for lead_id in inputs:
        profile = profiles.get(lead_id)
        if profile is None or not profile.website:
            continue
        if any(saved.contact.source is ContactSource.WEBSITE for saved in existing.get(lead_id, [])):
            continue
        targets[lead_id] = profile.website
    if not targets:
        return

    # 几家同时抓（homepage_workers 个）。fetch_homepage 不抛异常，打不开的 error 有值
    # 输出   {1388: Homepage(final_url='https://home.hushly.example', html=<首页 html>, error=None)}
    with ThreadPoolExecutor(max_workers=config.homepage_workers) as pool:
        pages = dict(zip(targets, pool.map(website.fetch_homepage, targets.values()), strict=True))

    with conn.cursor() as cur:
        for lead_id, page in pages.items():
            if page.error or page.html is None or page.final_url is None:
                report.homepage_errors[lead_id] = page.error or "没有内容"
                print(f"[contacts] lead={lead_id} 官网打不开 {targets[lead_id]}：{page.error}")
                continue

            # 输入   跳转之后落到的 'https://home.hushly.example'
            # 输出   'hushly.example'。取不出来（空串、没有点）或者是共用平台（linktr.ee、instagram.com），
            #        这一页就不是这家自己的网站：不找邮箱、不写 leads.domain，记进 report.shared_hosts
            domain = website.registrable_domain(page.final_url)
            if not domain or "." not in domain or domain in config.shared_hosts:
                report.shared_hosts[lead_id] = domain
                print(f"[contacts] lead={lead_id} 官网落在 {page.final_url}，不是这家自己的网站，跳过")
                continue

            # 输入   首页 html，跳转之后的 'https://home.hushly.example'
            # 输出   ('contact@hushly.example',)
            emails = website.extract_emails(page.html, page.final_url)
            for contact in website.to_contacts(emails, page.final_url):
                if contacts_db.insert_contact(
                    cur, lead_id, contact, person_lookup_id=None, run_id=run_id, found_at=now
                ):
                    report.emails_from_websites += 1
                    _note_changed_email(report, emails_before, lead_id, contact.value)

            # 输入   domain='hushly.example'
            # 输出   DomainWrite(outcome='written')；已有值是 'kept'；被别家占了是 ('taken', 那一家的 id)
            written = contacts_db.set_domain_if_empty(cur, lead_id, domain)
            if written.outcome == "taken":
                report.domain_conflicts[lead_id] = written.other_lead_id
                print(
                    f"[contacts] lead={lead_id} 的官网域名已经是 lead={written.other_lead_id} 的了，"
                    "两行可能是同一家公司，没写，等人看"
                )
    conn.commit()


def _note_changed_email(
    report: ContactsReport, emails_before: dict[int, list[str]], lead_id: int, value: str
) -> None:
    """刚写进 lead_contacts 的一个邮箱，如果这家以前有别的邮箱、而这个地址以前没有，记进报告。

    输入：报告；这一次开始前每家已有的邮箱，如 {1388: ["contact@hushly.example"]}；这家的 lead_id；
          刚写进去的地址，如 "sam@hushly.example"
    输出：无。report.changed_emails 可能多一笔

    三种（比地址不分大小写）：

        这家以前一个邮箱都没有     不记：第一次找到，没有旧的可比
        这个地址以前就有           不记：换了个来源又找到同一个地址
        以前有别的、这个是新的     记：{1388: (["sam@hushly.example"], ["contact@hushly.example"])}
    """
    old = emails_before.get(lead_id)
    if not old:
        return
    if value.lower() in {v.lower() for v in old}:
        return

    # 输出   report.changed_emails[1388] = (["sam@hushly.example"], ["contact@hushly.example"])
    new, _ = report.changed_emails.setdefault(lead_id, ([], list(old)))
    if value.lower() not in {v.lower() for v in new}:
        new.append(value)


def card_contacts(cur: Any, lead_ids: Sequence[int]) -> dict[int, CardContacts]:
    """拼卡片时用：这几家的邮箱、官网、公司概况，和每个作者跟这家是什么关系。只读，不花钱。

    输入：游标；这一批要出卡片的公司，如 [1388, 1285]
    输出：lead_id -> CardContacts，传进来的每一家都有（库里没有这家的是 NO_CONTACTS），如

        {1388: CardContacts(website_url="https://hushly.example",
                            company_page_url="https://www.linkedin.com/company/hushly/",
                            profile=CompanyProfile(employee_count=11, ...),
                            emails=(CardEmail(value="contact@hushly.example", ...),
                                    CardEmail(value="sam@hushly.example", ...)),
                            people=(CardPerson(name="sam hadid", relation=CardRelation.MEMBER,
                                               position="Founder", ...),))}

    读的跟 find_contacts 第 ① 步是同一套（contact_inputs / latest_profiles / person_lookups_for /
    contacts_for），多做的是给每个作者判关系：先用 classify_author 判出 P 几，停在第三关（P3 / P4）的
    再看 person_lookups 里有没有他：没有是 NOT_LOOKED_UP，有且 matches_lead=true 是 MEMBER，
    有但 false 是 LEFT（P9）。还没跑过 find_contacts 的那家，作者那几行照样出得来（只看 signals），
    只是没有邮箱和概况
    """
    config = load_contacts_config()
    inputs = contacts_db.contact_inputs(cur, lead_ids)
    ids = list(inputs)
    profiles = contacts_db.latest_profiles(cur, ids)
    lookups = contacts_db.person_lookups_for(cur, ids)
    saved = contacts_db.contacts_for(cur, ids)

    out: dict[int, CardContacts] = {lead_id: NO_CONTACTS for lead_id in lead_ids}
    for lead_id, (lead, authors) in inputs.items():
        # 按主页链接找查人那一行
        # 输出   {'https://www.linkedin.com/in/sam-hadid-a1b2c3d4': SavedPersonLookup(...)}
        by_url = {
            harvestapi_profile.clean_profile_url(s.lookup.person_linkedin_url): s
            for s in lookups.get(lead_id, [])
        }
        # 输出   {<查人那一行的 id>: SavedPersonLookup(...)}，邮箱那一行靠 person_lookup_id 找回是谁的
        lookup_by_id = {s.id: s for s in lookups.get(lead_id, [])}

        people = _card_people(lead, authors, by_url=by_url, config=config)

        # 官网：leads.domain（跳转之后的真域名）优先，没有就用概况里登记的地址
        # 输出   'https://hushly.example'；只有概况时 'http://orbobrand.example'（登记的原样）；都没有 None
        profile = profiles.get(lead_id)
        if lead.domain:
            website_url = f"https://{lead.domain}"
        elif profile and profile.website:
            website_url = profile.website
        else:
            website_url = None
        # 输出   'hushly.example'；Kestrel 这种不知道官网的是 None
        lead_domain = website.registrable_domain(website_url) if website_url else None

        # 输入   saved[1388]，最新的在前
        # 输出   (CardEmail(value="contact@hushly.example", source=WEBSITE, person_name=None, ...), ...)，只要邮箱
        emails = []
        for c in saved.get(lead_id, []):
            if c.contact.channel != ContactChannel.EMAIL:
                continue
            found = lookup_by_id.get(c.person_lookup_id) if c.person_lookup_id else None
            emails.append(
                CardEmail(
                    value=c.contact.value,
                    source=c.contact.source,
                    source_url=c.contact.source_url,
                    person_name=found.lookup.person_name if found else None,
                    email_status=c.contact.email_status,
                    catch_all=c.contact.catch_all,
                    found_in_google=c.contact.found_in_google,
                    quality_score=c.contact.quality_score,
                    other_company=_other_company(c.contact.value, found, lead, lead_domain),
                )
            )

        # 输出   'https://www.linkedin.com/company/hushly/'；只有公司 id 没有 slug 就用 id；C2 是 None
        slug_or_id = lead.linkedin_slug or lead.linkedin_company_id
        company_page_url = f"https://www.linkedin.com/company/{slug_or_id}/" if slug_or_id else None

        out[lead_id] = CardContacts(
            website_url=website_url,
            company_page_url=company_page_url,
            profile=profile,
            emails=tuple(emails),
            people=people,
        )
    return out


def _other_company(
    email: str,
    found: SavedPersonLookup | None,
    lead: LeadForContacts,
    lead_domain: str | None,
) -> str | None:
    """个人主页来的这个邮箱，是不是他另一份现职那家的邮箱。是就给那家的名字，不是给 None。

    输入：邮箱；这个邮箱是替哪一行查人查出来的（官网来的邮箱没有这一行，是 None）；
          这家公司；这家官网的域名（不知道官网是 None）
    输出：'Rawcut' 或 None

    三种情况：

        一、Evan Park 替 Kestrel（真例子）
            输入   'evan.park@rawcutstudio.example'，Kestrel 不知道官网
                   他的现职：Rawcut（官网 https://rawcutstudio.example/）、Kestrel（没登记官网）
            判断   邮箱域名 rawcutstudio.example，这家的官网对不上（不知道也算对不上）；
                   Kestrel 那份是这家自己，跳过；Rawcut 那份的官网域名一样
            输出   'Rawcut'

        二、sam hadid 替 Hushly
            输入   'sam@hushly.example'，Hushly 的官网 hushly.example
            判断   邮箱域名就是这家的官网
            输出   None

        三、域名哪一家都对不上（比如 gmail.com，或者他没登记官网的那家）
            输出   None。说不清是哪家的就不写，不猜
    """
    if found is None:
        return None
    # 输入   'evan.park@rawcutstudio.example'
    # 输出   'rawcutstudio.example'。只传 @ 后面那一段：registrable_domain 不认 @
    email_domain = website.registrable_domain(email.rsplit("@", 1)[-1])
    if email_domain == lead_domain:
        return None

    for job, job_website in harvestapi_profile.current_jobs_with_websites(found.lookup.raw):
        # 这家自己那一份不算「另一份现职」
        if job_matches_lead(lead, job) or not job_website:
            continue
        if website.registrable_domain(job_website) == email_domain:
            return job.company_name
    return None


def _card_people(
    lead: LeadForContacts,
    authors: list[AuthorRef],
    *,
    by_url: dict[str, SavedPersonLookup],
    config: ContactsConfig,
) -> tuple[CardPerson, ...]:
    """这家每条证据里的每个作者，判出跟这家的关系，同一个人（或同一个主页）只留一行。

    输入：这家的身份；作者（帖子新的在前）；这家查过的人，按主页链接；配置（P8 那两条）
    输出：(CardPerson(name="sam hadid", relation=CardRelation.MEMBER, position="Founder", ...),)
          同一个人出现在几条证据里，只留最新那条帖子的链接
    """
    people: list[CardPerson] = []
    seen: set[str] = set()
    for author in authors:
        # 个人主页去掉 ?miniProfileUrn= 当键；公司主页原样
        # 输出   'https://www.linkedin.com/in/sam-hadid-a1b2c3d4'
        key = author.url if author.is_company_page else harvestapi_profile.clean_profile_url(author.url)
        if key in seen:
            continue
        seen.add(key)

        kind = classify_author(
            lead, author, common_words=config.common_words, max_short_length=config.max_short_length
        )
        position = current = None

        # 输入   AuthorKind（P 几）；停在第三关的再看查没查过、对没对上
        # 输出   CardRelation
        if kind is AuthorKind.OWN_PAGE:
            relation = CardRelation.OWN_PAGE
        elif kind is AuthorKind.OTHER_PAGE:
            relation = CardRelation.OTHER_PAGE
        elif kind is AuthorKind.PARTIAL_NAME:
            relation = CardRelation.UNCLEAR
        elif kind is AuthorKind.NAMED_IN_HEADLINE:
            found = by_url.get(key)
            if found is None:
                relation = CardRelation.NOT_LOOKED_UP
            elif found.lookup.matches_lead:
                relation = CardRelation.MEMBER
                # 职位从查人那一行的原始返回里取：person_lookups 只存了现职的公司，没存职位
                # 输出   'Founder'
                job, _ = pick_current_job(lead, harvestapi_profile.to_person(found.lookup.raw).current_jobs)
                position = job.position if job else None
            else:
                relation = CardRelation.LEFT
                current = found.lookup.current_company_name
        else:
            # NOT_NAMED（P5）和 NAME_TOO_COMMON（P8）卡片上一样写
            relation = CardRelation.NOT_MEMBER

        # 卡片上印的主页链接。公司主页的作者链接是它的帖子列表，换成公司页本身
        # 输入   'https://www.linkedin.com/company/singalong-group/posts'
        # 输出   'https://www.linkedin.com/company/singalong-group/'；链接里没有 /company/ 的原样
        profile_url = key
        if author.is_company_page and (slug := linkedin_company_slug(author.url)):
            profile_url = f"https://www.linkedin.com/company/{slug}/"

        people.append(
            CardPerson(
                name=author.name,
                relation=relation,
                position=position,
                current_company=current,
                profile_url=profile_url,
                post_url=author.post_url,
            )
        )
    return tuple(people)

