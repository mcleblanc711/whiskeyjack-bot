# Source alpha: pre-registration (D63)

Written 2026-11-15, before any experiment arm has been run or paid for. This file fixes what
will be measured, how, and when the results will be read. A later change is added under
[Amendments](#amendments) with its date and reason; the text above that section is not edited.

## Purpose

whiskeyjack records, for every forecast, the exact evidence packet the model was shown, which
documents it cited, and the outcome. That supports two statements about a source: it was
supplied, and the model said it used it. It does not support the two that matter: that the
source changed the forecast, or that it made the forecast better.

This study measures those two, for classes of source, on evidence that was retrieved and
stored before each question's outcome existed.

## Definitions

- **Exposure.** A document was in the packet the model was shown.
- **Declared use.** The model cited the document in an evidence adjustment or a load-bearing
  fact.
- **Influence.** The change in the forecast when the source is removed, measured in logits
  for a binary forecast. Influence has no sign of merit: a source can move a forecast the
  wrong way.
- **Value (source alpha).** The change in proper score from including a source or source
  class, under a stated model, information cutoff and research budget. Positive means the
  forecast scored better with the source than without it.
- **Legitimacy.** A label known before the outcome: publisher class, the provider's own
  signals, freshness, whether the publisher is the question's named resolution source.
- **Source cluster.** The documents in one packet that are copies of one report (M1-321).
  Clusters, not documents, are the unit that is included or removed.

A source class is **good** if including it improves scores out of sample. A label is a
**valid quality signal** if it predicts value.

## Data

- **Population.** Every forecast this account posted to a MiniBench project whose question
  has a scorable resolution, and whose stored request rebuilds byte-identically (the fidelity
  gate of D62). On 2026-11-15 that is at most 229 forecasts: 113 binary, 56 numeric, 49
  discrete, 11 multiple choice. Excluded records are counted by reason.
- **Growth.** About 60 questions per two-week round. New resolved questions join the
  population only at the analysis points below.
- **Evidence.** The stored packet behind each forecast, read back from the ledger. No
  document is retrieved again for a replay arm, so nothing dated after the forecast can enter.
- **Model.** `openrouter/openai/gpt-6-astra` at high reasoning effort, the live forecaster.
  A different model may be used only for a question that opened after that model entered
  service. The model identifier the provider reports is stored on every row.
- **Prompt.** The live tournament prompt at the version current when an arm starts. Every
  arm in one comparison uses the same prompt bytes.

## Research questions and arms

Each arm changes one thing against the control. The control is always re-run under the arm's
own model, effort and prompt; the stored live forecast is never used as a control.

| | Question | Arm | Compared with |
|---|---|---|---|
| RQ1 | Does retrieved evidence improve forecasts, and by how much? | A1: no research documents | A0 |
| RQ2 | Which source classes carry the value? | A2: random subsets | within A2 |
| RQ3 | Does declared use track influence? | A2, at cluster level | within A2 |
| RQ4 | Does the publisher's identity matter to the model? | A3: publisher masked | A0 |
| RQ5 | Do syndicated copies move forecasts without adding value? | A4: one document per cluster | A0 |
| RQ6 | Which legitimacy labels predict value? | A2, with labels | within A2 |
| RQ7 | Do new source types add value? | Live paired arm (M1-372) | its control |

- **A0, control.** The full stored packet, three independent samples per question. It gives
  the sample-to-sample variance (the noise floor) and the reference for A1, A3, A4 and A5.
- **A1, no research.** The question alone.
- **A2, random subsets.** Six subsets per question. Each source cluster is kept independently
  with probability one half, from a seed fixed by the question id and the subset index. A
  subset with no cluster kept is allowed.
- **A3, publisher masked.** The full packet with publisher names and URL hosts replaced by
  neutral tokens in the model input. Stored provenance is untouched.
- **A4, syndication collapsed.** One document kept per cluster, run only on questions whose
  packet has a cluster of two or more.
- **A5, order shuffled.** The full packet in a seeded random order. A control for position,
  reported beside the others and not a hypothesis of its own.
- **A6, provider drops.** The packet without AskNews documents, and without Exa documents.
  A robustness check on RQ2 in the leave-one-out form.

In every arm a document keeps the source id it had in the full packet.

## Hypotheses

Stated so that each can fail.

- **H1 (RQ1).** A0 scores better than A1.
- **H2 (RQ2).** Documents from the question's named resolution source have higher value
  than documents from other publishers.
- **H3 (RQ3).** Clusters the model cited have larger influence than clusters it did not, and
  clusters behind a load-bearing fact have larger influence than other cited clusters.
- **H4 (RQ4).** Masking the publisher changes forecasts by no more than the noise floor.
  This is the published finding for small models; the test is whether it holds here.
- **H5 (RQ5).** Collapsing syndicated copies moves forecasts (influence above the noise
  floor) without lowering scores.
- **H6 (RQ6).** Publisher class and resolution-source match predict cluster value; the
  provider's popularity-style signals do not.
- **H7 (RQ7).** The extended packet scores better than the baseline packet.

## Measures

- **Primary: local log score.** Natural log of the probability assigned to the outcome, for
  every question type (M4-802, M4-809). For two forecasts on one question, 100 times the
  difference is the difference in Metaculus spot peer score, halved for continuous questions,
  because the other forecasters' scores cancel. Results are reported in those points.
- **Secondary: Brier score** for binary and multiple-choice questions.
- **Influence:** the absolute difference in logits for binary forecasts; for other types,
  the difference in log score against the control's own distribution is reported as
  descriptive only.

## Analysis

- **Unit and clustering.** The unit is a forecast question. Questions that belong to one
  post are not independent, so every interval resamples posts.
- **Arm against control (RQ1, RQ4, RQ5).** The paired difference per question is the arm's
  score minus the mean score of the control's three samples. The estimate is the mean over
  questions; the interval is a percentile bootstrap over posts, 10,000 resamples, fixed seed.
- **Random subsets (RQ2).** Score is regressed on, for each class, the share of that class's
  clusters kept in the subset, with a fixed effect per question. A class's coefficient is the
  score change from keeping all of its clusters against none, averaged over what else was
  kept. Classes are the publisher classes of M1-369 plus resolution-source match. A class
  present in fewer than 30 questions is reported and not tested. Coefficients are also
  reported with partial pooling across classes.
- **Cluster level (RQ3, RQ6).** A cluster's influence is the mean forecast with it kept
  minus the mean without it, across the six subsets; its value is the same contrast in
  score. With six subsets these are noisy for one cluster and are only ever summarized over
  many. RQ3 compares cited with uncited and load-bearing with other cited clusters. RQ6
  regresses cluster value on the labels with a fixed effect per question.
- **Noise floor.** The within-question standard deviation across A0's samples. An influence
  is described as present only when its interval excludes the range the noise floor gives.
- **Multiple comparisons.** The primary tests are H1, H2, H3 (two contrasts), H4, H5, H6
  (one test per label) and H7. False-discovery rate is controlled across them by
  Benjamini-Hochberg at 0.10. Everything else is labelled exploratory.
- **Power.** The minimum detectable effect of each arm is computed from A0's variance and
  added as the first amendment, before any other arm's results are read.

## Analysis points and stopping

- **Replay arms.** Each arm runs once over the population as it stands when the arm starts.
  Results are read at two points only: when the first grid (A0 to A6) is complete, and when
  the population reaches 450 resolved questions, at which point each arm is extended to the
  new questions. No arm is extended or re-run because of what its result was.
- **Live paired arm (RQ7).** Read for a decision after 120 resolved paired questions, and
  finally at 240. The treatment is submitted throughout.
- **Budget.** Each arm runs under an explicit spend ceiling (D64). An arm stopped by its
  ceiling is reported as incomplete with the questions it covered, chosen in record order.

## Reported whatever the result

- Every arm, every class and every test above, with n, estimate and interval, including
  nulls and results against the hypothesis.
- The noise floor, the number of records excluded by the fidelity gate and why, and the cost
  of each arm.
- The control's absolute scores by question type, so a reader can see the baseline.

## Limits stated in advance

- One model and one prompt. Value is defined relative to them and may not transfer.
- MiniBench questions are generated and resolved automatically and run about ten days.
- The live forecasts were made at an earlier effort and prompt; this is why controls are
  re-run and never read from the live record.
- A packet holds only what two providers returned. A class that was never retrieved has no
  estimate, which is a statement about coverage and not about that class's value.
- Replay cannot test a new retrieval method without leaking the outcome. RQ7 is prospective
  for that reason.
- Removing a document also removes the context it gave other documents. The random-subset
  design averages over this; it does not separate it.
- The provider may change the model behind an identifier. The reported identifier is stored
  per row and a change between arms is reported.

## Release

The released dataset holds question ids and URLs, document URLs and publisher domains,
labels with their versions, content hashes, timestamps, source ids, forecasts, outcomes, arm
specifications and scores. It holds no article text. The terms of AskNews, Exa and Metaculus
are checked before anything is published.

## Related work

Halawi et al. 2024 (retrieval-augmented forecasting; arXiv 2402.18563). ForecastBench (arXiv
2409.19839). Bench to the Future (frozen corpora; arXiv 2506.21558). Pitfalls in Evaluating
Language Model Forecasters (arXiv 2506.00723). The Corroboration Illusion (injected articles
and ignored publisher labels in small models; arXiv 2609.22246). Metaculus's FutureEval
analyses, 2024 to 2026. ContextCite (attribution by random context ablation), which the
random-subset arm adapts from generated text to forecast scores.

## Amendments

None yet.
