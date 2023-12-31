# Declarative Labeler

Esta action apresenta uma interface declarativa para a adição e remoção de labels em pull requests.

### Funcionalidades

- Checar a quantidade de approves;
- Checar tipo de PR (draft vs pronto);
- Adicionar e remover labels específicas quando as condições forem atendidas;
- Inverter as regras e as consequências quando as condições falharem;
    - Exemplo: re-adicionar a label `waiting review` quando uma *review* for *dismissed*;

## Exemplos de Workflow

### Adicionar `waiting review` quando um PR é aberto / convertido de um draft 

```yml
on:
  pull_request:
    types:
      - opened
      - reopened
      - ready_for_review
      - converted_to_draft

jobs:
  add_waiting_review_label:
    runs-on: ubuntu-latest
    steps:
      - name: Mark PR as ready by adding/removing 'waiting review' label
        uses: iclinic/automations/declarative-labeler@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          when_pr_is: ready
          then_add_labels: waiting review
          also_reverse: true
```

#### Explicação linha-a-linha

```yml
github_token: ${{ secrets.GITHUB_TOKEN }}
```
Concede permissão a action que realize edições no pull request caso o usuário que a acionou (quem abriu o PR) tenha permissões de escrita no repositório.

```yml
when_pr_is: ready
```
Exclui PRs que estejam em **draft**;


```yml
then_add_labels: 'waiting review'
```
Adiciona a label `waiting review` quando as condições (`when_pr_is: ready`) forem atendidas;

```yml
also_reverse: true
```
Executa a consequência inversa no caso das condições não serem atendidas. Neste caso, a label `waiting review` será removida caso o PR seja transformado em **draft**.

---

### Adicionar `waiting QA` e remover `waiting review` quando o PR for aprovado (e o contrário quando as revisões forem removidas)

```yml
on:
  pull_request_review:
    types:
      - submitted
      - dismissed

  pull_request:
    types:
      - review_requested

jobs:
  juggle_labels_on_approve:
    runs-on: ubuntu-latest
    steps:
      - name: Add and Remove 'waiting QA' and 'waiting review' labels
        uses: iclinic/automations/declarative-labeler@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          when_review_status: APPROVED
          then_add_labels: waiting QA,has dependencies
          then_remove_labels: waiting review,good first issue
          also_reverse: true
```

Neste exemplo, caso as aprovações sejam *dismissed* ou *re-requested*, a action recolocará a label `waiting review` e removerá a label `waiting QA`. Novamente, isso é efeito puramente da linha `also_reverse: true`
