<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A migração de dados do lado PostgreSQL: `INSERT` e `DELETE` com parâmetros
 * nomeados. As duas saem `controlled`, como migração de dados, com a tabela na
 * razão e nenhum valor. O `DELETE` tem `WHERE`; sem ele seria `breaking`, como
 * o `TRUNCATE`.
 *
 * Migração de dados é onde dado de cliente entra numa migração, e a razão vai
 * para um canal do Slack. `tests/test_pii.py` planta o dado nesta mesma posição.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120300 extends AbstractMigration
{
    private const DEFAULT_CARRIER = 'none';

    public function getDescription(): string
    {
        return 'Semeia e limpa a transportadora padrao';
    }

    public function up(Schema $schema): void
    {
        $this->addSql(
            'INSERT INTO stock."freight_note" (carrier_code, net_amount) VALUES (:code, :amount)',
            [
                'code' => self::DEFAULT_CARRIER,
                'amount' => 0,
            ]
        );

        $this->addSql(
            'DELETE FROM stock."freight_note" WHERE carrier_code = :code',
            ['code' => self::DEFAULT_CARRIER]
        );
    }

    public function down(Schema $schema): void
    {
        $this->addSql(
            'DELETE FROM stock."freight_note" WHERE carrier_code = :code',
            ['code' => self::DEFAULT_CARRIER]
        );
    }
}
