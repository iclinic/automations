<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * O dialeto PostgreSQL do consumidor, com o que o distingue do MySQL:
 * identificador entre aspas duplas e nome de tabela qualificado por schema. As
 * aspas são apagadas pela máscara do `detect/sql.py` e removidas do nome que a
 * razão cita, então a frase sai com o nome limpo — é o que impede uma aspa de
 * chegar ao Slack.
 *
 * O `CREATE UNIQUE INDEX` está aqui de propósito: é `breaking`, não `safe`, e é a
 * linha que o corpus real do consumidor alcança duas vezes.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120000 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Cria a tabela de posicoes de armazem';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('CREATE TABLE stock."warehouse_bin" (
            "id" SERIAL PRIMARY KEY,
            "bin_label" varchar(40) NULL,
            "capacity" integer NULL
        )');

        $this->addSql('CREATE UNIQUE INDEX uq_freight_note_carrier ON stock."warehouse_bin" ("bin_label")');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('DROP TABLE stock."warehouse_bin"');
    }
}
