import {MigrationInterface, QueryRunner} from "typeorm";

export class UnreadSql1700000000007 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query("ALTER TABLE schedule DROP COLUMN seats");
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
