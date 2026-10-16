<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A forma dominante do banco MySQL do consumidor: um `CREATE TABLE` inteiro
 * dentro de um **nowdoc** (`<<<'SQL'`), que é a segunda das duas sintaxes
 * literais do PHP e a única forma multilinha que o classificador lê.
 *
 * E o par que o cartão exige: o `DROP TABLE` está no `down()`, que é onde a
 * QQ-2161 achou o mesmo problema no Alembic. `up()` sai `safe`, e é isso que
 * prova que o rollback não pinta a migração de vermelho.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120000 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Cria a tabela de notas de frete';
    }

    public function up(Schema $schema): void
    {
        $this->addSql(<<<'SQL'
CREATE TABLE freight_note (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    carrier_code VARCHAR(40) NOT NULL,
    net_amount DECIMAL(15,2) NOT NULL,
    settled TINYINT(1) NULL,
    PRIMARY KEY (id),
    KEY idx_freight_note_carrier (carrier_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
SQL);
    }

    public function down(Schema $schema): void
    {
        $this->addSql('DROP TABLE freight_note');
    }
}
