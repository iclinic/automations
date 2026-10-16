<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * `DROP TABLE` em `up()`, no dialeto MySQL. É a operação mais destrutiva do
 * vocabulário e o corpus real do consumidor não tem nenhuma — as duas que
 * existem lá estão no `down()`, que o parser ignora por especificação.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250101120300 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Descarta a tabela de posicoes de armazem';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('DROP TABLE warehouse_bin');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('CREATE TABLE warehouse_bin (id INT UNSIGNED NOT NULL, bin_label VARCHAR(40) NULL)');
    }
}
